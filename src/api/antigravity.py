"""
Antigravity API Client - Handles communication with Google's Antigravity API
"""

import asyncio
import hashlib
import json
import re
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Callable, Tuple

from fastapi import Response
from config import (
    get_antigravity_api_url,
    get_auto_ban_error_codes,
    get_request_log_enabled,
    get_request_log_error_codes,
    get_usage_log_enabled,
)
from log import log

from src.credential_manager import (
    add_credential,
    get_valid_credential,
)
from src.httpx_client import stream_post_async, post_async
from src.models import Model, model_to_dict
from src.utils import ANTIGRAVITY_USER_AGENT
from src.session_cache import derive_session_key
from src.usage_log import (
    extract_usage_tokens,
    new_request_id,
    record_usage_log,
)
from src.request_body_log import record_request_body

# Import common base utilities
from src.api.utils import (
    handle_error_with_retry,
    get_retry_config,
    record_api_call_success,
    record_api_call_error,
    parse_and_log_cooldown,
)

# ==================== Global Credential Manager ====================


def _extract_first_user_text(request_payload: Dict[str, Any]) -> str:
    contents = request_payload.get("contents", [])
    if not isinstance(contents, list):
        return ""
    for content in contents:
        if not isinstance(content, dict) or content.get("role") != "user":
            continue
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
    return ""


def _generate_request_id() -> str:
    """Generate requestId in full format, aligned with the reference implementation:
    agent/{uuid}/{millisecond_timestamp}/{trajectory_id}/{step}
    """
    trajectory_id = str(uuid.uuid4())
    step = 1
    ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"agent/{uuid.uuid4()}/{ms}/{trajectory_id}/{step}"


def _build_labels(model: str, trajectory_id: str, step: int) -> Dict[str, str]:
    used_claude = "claude" in model.lower()
    return {
        "last_step_index": str(step),
        "model_enum": model,
        "trajectory_id": trajectory_id,
        "used_claude": str(used_claude).lower(),
        "used_claude_conservative": str(used_claude).lower(),
    }


def _should_forward_antigravity_header(header_name: str) -> bool:
    normalized = header_name.strip().lower()
    if not normalized:
        return False
    if normalized.startswith("x-b3-"):
        return True
    return normalized in {
        "accept-language",
        "traceparent",
        "tracestate",
        "x-cloud-trace-context",
        "x-goog-api-client",
        "x-goog-request-params",
        "x-goog-user-project",
        "x-request-id",
    }


def _sanitize_antigravity_headers(extra_headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    if not extra_headers:
        return {}
    sanitized: Dict[str, str] = {}
    for key, value in extra_headers.items():
        if _should_forward_antigravity_header(key):
            sanitized[key] = value
    return sanitized


async def wrap_cli_request(
    gemini_request: Dict[str, Any],
    model: str,
    project_id: str,
) -> Tuple[Dict[str, Any], str]:
    """
    Wrap Gemini format request into Antigravity CLI format.
    Returns (payload, request_id).
    """
    # Shallow copy + only rebuild layers that need modification (toolConfig/functionCallingConfig),
    # avoiding the hot-path overhead of a full deepcopy on large payloads (long conversation history);
    # unmodified fields like contents share references with caller (caller will not modify afterwards)
    inner = dict(gemini_request)
    first_user_text = _extract_first_user_text(inner)

    # Inject sessionId
    session_id = str(inner.get("sessionId") or "").strip()
    if not session_id:
        if first_user_text:
            digest = hashlib.sha256(first_user_text.encode("utf-8")).digest()
            session_id_val = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
            session_id = f"-{session_id_val}"
        else:
            session_id = f"-{uuid.uuid4().int % 9_000_000_000_000_000_000}"
        inner["sessionId"] = session_id

    # Inject labels
    inner["labels"] = _build_labels(model, session_id, 1)

    # toolConfig defaults to VALIDATED (rebuild these two layers to avoid mutating caller's dict)
    src_tool_config = inner.get("toolConfig") or {}
    src_func_config = src_tool_config.get("functionCallingConfig") or {}
    func_config = dict(src_func_config)
    func_config["mode"] = "VALIDATED"
    tool_config = dict(src_tool_config)
    tool_config["functionCallingConfig"] = func_config
    inner["toolConfig"] = tool_config

    request_id = _generate_request_id()

    payload = {
        "project": project_id,
        "requestId": request_id,
        "request": inner,
        "model": model,
        "requestType": "agent",
    }
    return payload, request_id


# ==================== Helper Functions ====================

def build_antigravity_headers(
    access_token: str,
    extra_headers: Optional[Dict[str, str]] = None,
    model_name: str = "",
) -> Dict[str, str]:
    """Build Antigravity CLI API request headers."""
    headers = {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        "Connection": "close",
        "requestId": f"req-{uuid.uuid4()}",
    }

    for key, value in _sanitize_antigravity_headers(extra_headers).items():
        headers.setdefault(key, value)

    # Determine request_type based on model name
    if model_name:
        if "image" in model_name.lower():
            headers["requestType"] = "image_gen"
        else:
            headers["requestType"] = "agent"

    return headers


def _is_retryable_status(status_code: int, disable_error_codes: List[int]) -> bool:
    """Determine whether the status code is retryable."""
    return status_code in (429, 503) or status_code in disable_error_codes


async def _switch_credential_for_retry(
    *,
    next_cred_task: Optional[asyncio.Task],
    retry_interval: float,
    refresh_credential_fast: Callable[[], Any],
    apply_cred_result: Callable[[Tuple[str, Dict[str, Any]]], bool],
    log_prefix: str,
) -> Tuple[bool, Optional[asyncio.Task]]:
    """Prefer pre-warmed credential; fall back to synchronous refresh upon failure."""
    if next_cred_task is not None:
        try:
            cred_result = await next_cred_task
            next_cred_task = None
            if cred_result and apply_cred_result(cred_result):
                await asyncio.sleep(retry_interval)
                return True, next_cred_task
        except Exception as e:
            log.warning(f"{log_prefix} Pre-warmed credential task failed: {e}")
            next_cred_task = None

    await asyncio.sleep(retry_interval)
    if await refresh_credential_fast():
        return True, next_cred_task

    return False, next_cred_task


# ==================== Streaming and Non-Streaming Request Functions ====================

def _derive_session_key(model_name: str, body: Dict[str, Any]) -> Optional[str]:
    """Derive session key from API request body (used in credential affinity cache mode).

    body format: {"model": ..., "request": <normalized Gemini request>}
    hash(model + systemInstruction or first user message) — the latter must be content after all formatting
    """
    inner = body.get("request") if isinstance(body.get("request"), dict) else body
    return derive_session_key(model_name, inner)


def _base_log_entry(
    model_name: str,
    email: str,
    is_stream: bool,
    log_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build common fields for request log entry.

    model_name: Actual model name used by backend (after mapping)
    log_ctx.client_model: Original model name requested by client (may have feature prefix)
    """
    ctx = log_ctx or {}
    return {
        "request_id": ctx.get("request_id") or new_request_id(),
        "api_format": ctx.get("api_format", ""),
        "client_ip": ctx.get("client_ip", ""),
        "model_name": ctx.get("client_model") or model_name,
        "upstream_model_name": model_name,
        "credential_email": email,
        "is_stream": is_stream,
        "api_key_name": ctx.get("api_key_name", ""),
        "api_key_hash": ctx.get("api_key_hash", ""),
    }


async def _record_request_body_detail(
    status_code: int,
    error_message: str,
    log_entry: Dict[str, Any],
    *,
    url: str,
    headers: Optional[Dict[str, str]],
    payload: Any,
    response_body: str,
    is_stream: bool,
) -> str:
    """
    Detailed request body log: written to disk (JSON file) when enabled and status code
    matches configured error codes. Database only stores file index path. Any exception
    does not affect the main flow.
    """
    try:
        if not await get_request_log_enabled():
            return ""
        if status_code not in await get_request_log_error_codes():
            return ""

        rel_path = await record_request_body(
            request_id=str(log_entry.get("request_id", "")),
            model_name=str(log_entry.get("model_name", "")),
            upstream_model_name=str(log_entry.get("upstream_model_name", "")),
            credential_email=str(log_entry.get("credential_email", "")),
            is_stream=is_stream,
            status_code=status_code,
            url=url,
            headers=headers,
            payload=payload,
            response_body=response_body,
            error_message=error_message,
        )
        return rel_path or ""
    except Exception as e:
        log.debug(f"Failed to write detailed request body log (does not affect main flow): {e}")
        return ""


async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    log_ctx: Optional[Dict[str, Any]] = None,
):
    """
    Streaming request function.

    Args:
        body: Request body
        native: Whether to return raw bytes stream; if False, returns str stream
        headers: Extra request headers
        log_ctx: Request log context (api_format / client_ip / request_id)

    Yields:
        Response object (on error) or bytes/str stream (on success)
    """
    model_name = body.get("model", "")

    # Request log: start time and base entry (recorded once for both success and failure)
    log_entry = _base_log_entry(model_name, "", True, log_ctx)
    log_start_time = time.time()

    # Credential affinity: derive session key from request content
    session_key = _derive_session_key(model_name, body)

    # 1. Get valid credential
    cred_result = await get_valid_credential(
        model_name=model_name, session_key=session_key
    )

    if not cred_result:
        # If return value is None, return 500 error
        log.error("[ANTIGRAVITY STREAM] No available credentials")
        log_entry.update({
            "status_code": 500, "success": False,
            "error_message": "No available credentials", "use_time": time.time() - log_start_time,
        })
        await record_usage_log(log_entry)
        yield Response(
            content=json.dumps({"error": "No available credentials"}),
            status_code=500,
            media_type="application/json"
        )
        return

    current_file, credential_data = cred_result
    log_entry["credential_email"] = current_file
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")

    if not access_token:
        log.error(f"[ANTIGRAVITY STREAM] No access token in credential: {current_file}")
        log_entry.update({
            "status_code": 500, "success": False,
            "error_message": "No access token in credential", "use_time": time.time() - log_start_time,
        })
        await record_usage_log(log_entry)
        yield Response(
            content=json.dumps({"error": "No access token in credential"}),
            status_code=500,
            media_type="application/json"
        )
        return

    # 2. Build URL and request headers (endpoint from config, supports hot update from dashboard)
    target_url = f"{await get_antigravity_api_url()}/v1internal:streamGenerateContent?alt=sse"

    auth_headers = build_antigravity_headers(access_token, headers, model_name)

    # Build CLI format request body
    inner_request = body.get("request", body)
    final_payload, _ = await wrap_cli_request(inner_request, model_name, project_id)

    # 3. Call stream_post_async to perform request
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # Error codes that disable credential
    last_error_response = None  # Record the last error response
    last_error_body = ""  # Detailed log: last error response body
    next_cred_task = None  # Pre-warmed next credential task

    # Internal function: quickly update credential (only updates token and project_id to avoid rebuilding entire request)
    async def refresh_credential_fast():
        nonlocal current_file, access_token, auth_headers, project_id, final_payload
        cred_result = await get_valid_credential(
            model_name=model_name, session_key=session_key
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        # Only update token and project_id, avoid rebuilding entire headers and payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, access_token, project_id, auth_headers, final_payload
        current_file, credential_data = cred_result
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    for attempt in range(max_retries + 1):
        success_recorded = False  # Flag indicating whether success has been recorded
        need_retry = False  # Flag indicating whether retry is needed
        usage_tokens: Dict[str, int] = {}  # Token usage extracted from stream
        attempt_start_time = time.time()  # Start time of current upstream call

        async def _log_stream_result(status_code: int, success: bool, error_message: str = "") -> None:
            """
            Write request log for one upstream call (each call to Google API produces one log;
            retries will record additional entries on subsequent attempts, which is normal).
            """
            # Detailed request body log: write to disk when matching configured error codes, DB only stores file index
            request_log_file = await _record_request_body_detail(
                status_code,
                error_message,
                log_entry,
                url=target_url,
                headers=auth_headers,
                payload=final_payload,
                response_body=last_error_body,
                is_stream=True,
            )

            # Record the actual credential used for each log entry
            await record_usage_log({
                **log_entry,
                "credential_email": current_file,
                "status_code": status_code,
                "success": success,
                "use_time": time.time() - attempt_start_time,
                "error_message": error_message[:500],
                "request_log_file": request_log_file,
                **usage_tokens,
            })

        try:
            async for chunk in stream_post_async(
                url=target_url,
                body=final_payload,
                native=native,
                headers=auth_headers
            ):
                # Check if chunk is a Response object
                if isinstance(chunk, Response):
                    status_code = chunk.status_code
                    last_error_response = chunk  # Record the last error

                    # Cache error parsing result to avoid repeated decoding
                    error_body = None
                    try:
                        error_body = chunk.body.decode('utf-8') if isinstance(chunk.body, bytes) else str(chunk.body)
                    except Exception:
                        error_body = ""
                    last_error_body = error_body or ""

                    # If status code is 429, 503, or in disable codes, record and retry
                    if _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                        log.warning(f"[ANTIGRAVITY STREAM] Streaming request failed (status={status_code}), credential: {current_file}, response: {error_body[:500] if error_body else 'none'}")

                        # Parse cooldown time
                        cooldown_until = None
                        if (status_code == 429 or status_code == 503) and error_body:
                            try:
                                cooldown_until = await parse_and_log_cooldown(error_body)
                            except Exception:
                                pass

                        # Pre-warm next credential
                        if next_cred_task is None and attempt < max_retries:
                            next_cred_task = asyncio.create_task(
                                get_valid_credential(
                                    model_name=model_name, session_key=session_key
                                )
                            )

                        # Record error and switch credential
                        await record_api_call_error(current_file, status_code, cooldown_until, model_name=model_name,
                            error_message=error_body
                        )
                        # One log entry produced for this upstream call (retries will record entries respectively)
                        await _log_stream_result(status_code, False, error_body[:500] or f"HTTP {status_code}")

                        # Check whether to retry
                        should_retry = await handle_error_with_retry(status_code, current_file, retry_config["retry_enabled"], attempt, max_retries, retry_interval)

                        if should_retry and attempt < max_retries:
                            need_retry = True
                            break  # Break inner loop, prepare for retry
                        else:
                            # Do not retry, directly return original error
                            log.error(f"[ANTIGRAVITY STREAM] Reached maximum retries or retry not eligible, returning original error")
                            yield chunk
                            return
                    else:
                        # Error code is not in disable codes, return directly without retry
                        log.error(f"[ANTIGRAVITY STREAM] Streaming request failed with non-retryable status (status={status_code}), credential: {current_file}, response: {error_body[:500] if error_body else 'none'}")
                        await record_api_call_error(current_file, status_code, None, model_name=model_name,
                            error_message=error_body
                        )
                        await _log_stream_result(status_code, False, error_body[:500] or f"HTTP {status_code}")
                        yield chunk
                        return
                else:
                    # Not a Response, meaning genuine stream chunk, yield directly
                    # Only record success on the first chunk
                    if not success_recorded:
                        await record_api_call_success(current_file, model_name=model_name)
                        success_recorded = True
                        log.debug(f"[ANTIGRAVITY STREAM] Started receiving streaming response, model: {model_name}")

                    # Extract token usage from stream chunk (hot-path optimization):
                    # usageMetadata almost always appears only in the final stream chunk.
                    # Use low-overhead substring check first; only do full JSON parse on match
                    # to avoid calling json.loads twice per chunk.
                    if native is False and "usageMetadata" in chunk:
                        extracted = extract_usage_tokens(chunk)
                        if extracted:
                            usage_tokens = extracted

                    yield chunk

            # Streaming request finished, verify result
            if success_recorded:
                log.debug(f"[ANTIGRAVITY STREAM] Streaming response completed, model: {model_name}")
                await _log_stream_result(200, True)
                return
            elif not need_retry:
                # Did not receive any data (empty response), retry needed
                log.warning(f"[ANTIGRAVITY STREAM] Received empty response with no content, credential: {current_file}")
                await record_api_call_error(current_file, 200, None, model_name=model_name,
                    error_message="Empty response from API"
                )
                # One log entry produced for this upstream call (empty response)
                await _log_stream_result(200, False, "Empty response from API")

                if attempt < max_retries:
                    need_retry = True
                else:
                    log.error(f"[ANTIGRAVITY STREAM] Empty response reached maximum retries")
                    yield Response(
                        content=json.dumps({"error": "Upstream service returned empty response"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
            
            # Unified retry handling
            if need_retry:
                log.info(f"[ANTIGRAVITY STREAM] Retrying request (attempt {attempt + 2}/{max_retries + 1})...")

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY STREAM]",
                )
                if not switched:
                    log.error("[ANTIGRAVITY STREAM] No available credentials or token on retry")
                    yield Response(
                        content=json.dumps({"error": "No available credentials"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
                continue

        except GeneratorExit:
            # Downstream closed this generator early (converter stream cut, truncation recovery, client disconnect, etc.).
            # If upstream data was already received but did not reach normal completion, record a success log to prevent
            # request log loss (issue #438); GeneratorExit must not be swallowed and must be re-raised.
            if success_recorded:
                log.debug("[ANTIGRAVITY STREAM] Generator closed early, recording success log")
                await _log_stream_result(200, True)
            raise

        except Exception as e:
            log.error(f"[ANTIGRAVITY STREAM] Streaming request exception: {e}, credential: {current_file}")
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY STREAM] Retrying after exception (attempt {attempt + 2}/{max_retries + 1})...")
                # One log entry produced for this upstream call (exception)
                await _log_stream_result(500, False, f"Exception: {e}")
                await asyncio.sleep(retry_interval)
                continue
            else:
                # All retries failed, return the last error (if any)
                log.error(f"[ANTIGRAVITY STREAM] All retries failed, last exception: {e}")
                await _log_stream_result(500, False, f"Exception: {e}")
                if last_error_response:
                    yield last_error_response
                else:
                    # If no error response was recorded, return 500 error
                    yield Response(
                        content=json.dumps({"error": f"Streaming request exception: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )
                return

    # All retries exhausted (for loop ended normally) — each upstream call already recorded its log,
    # so return the last error response directly without extra logging
    log.error("[ANTIGRAVITY STREAM] All retries failed")
    if last_error_response:
        yield last_error_response
    else:
        yield Response(
            content=json.dumps({"error": "Request failed, all retries exhausted"}),
            status_code=429,
            media_type="application/json"
        )


async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    log_ctx: Optional[Dict[str, Any]] = None,
) -> Response:
    """
    Non-streaming request function.

    Args:
        body: Request body
        headers: Extra request headers
        log_ctx: Request log context (api_format / client_ip / request_id)

    Returns:
        Response object
    """
    model_name = body.get("model", "")

    # Request log: start time and base entry (recorded once for both success and failure)
    log_entry = _base_log_entry(model_name, "", False, log_ctx)
    log_start_time = time.time()

    # Detailed log context (upstream URL/headers/body; empty before request dispatch)
    target_url = ""
    auth_headers: Optional[Dict[str, str]] = None
    final_payload: Dict[str, Any] = {}
    last_error_body = ""  # Detailed log: last error response body
    current_file = ""  # Currently used credential (empty before acquisition)
    attempt_start_time = time.time()  # Start time of current upstream call (reset each loop)

    async def _log_result(status_code: int, success: bool, response_content: Any = None, error_message: str = "") -> None:
        """
        Write request log for one upstream call (each call to Google API produces one log;
        retries will record additional entries on subsequent attempts, which is normal).
        """
        # Detailed request body log: write to disk when matching configured error codes, DB only stores file index
        request_log_file = await _record_request_body_detail(
            status_code,
            error_message,
            log_entry,
            url=target_url,
            headers=auth_headers,
            payload=final_payload,
            response_body=last_error_body,
            is_stream=False,
        )

        # Skip token parsing when usage_log is disabled (avoids unnecessary full JSON parsing)
        tokens = {}
        if success and await get_usage_log_enabled():
            tokens = extract_usage_tokens(response_content)
        # Record the actual credential used for each log entry
        await record_usage_log({
            **log_entry,
            "credential_email": current_file,
            "status_code": status_code,
            "success": success,
            "use_time": time.time() - attempt_start_time,
            "error_message": error_message[:500],
            "request_log_file": request_log_file,
            **tokens,
        })

    # Credential affinity: derive session key from request content
    session_key = _derive_session_key(model_name, body)

    # 1. Get valid credential
    cred_result = await get_valid_credential(
        model_name=model_name, session_key=session_key
    )

    if not cred_result:
        # If return value is None, return 500 error
        log.error("[ANTIGRAVITY] No available credentials")
        await _log_result(500, False, error_message="No available credentials")
        return Response(
            content=json.dumps({"error": "No available credentials"}),
            status_code=500,
            media_type="application/json"
        )

    current_file, credential_data = cred_result
    log_entry["credential_email"] = current_file
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")

    if not access_token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {current_file}")
        # Record failure log (including the credential involved)
        await _log_result(500, False, error_message="No access token in credential")
        return Response(
            content=json.dumps({"error": "No access token in credential"}),
            status_code=500,
            media_type="application/json"
        )

    # 2. Build URL and request headers (endpoint from config, supports hot update from dashboard)
    target_url = f"{await get_antigravity_api_url()}/v1internal:generateContent"

    auth_headers = build_antigravity_headers(access_token, headers, model_name)

    # Build CLI format request body
    inner_request = body.get("request", body)
    final_payload, _ = await wrap_cli_request(inner_request, model_name, project_id)

    # 3. Call post_async to perform request
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # Error codes that disable credential
    last_error_response = None  # Record the last error response
    next_cred_task = None  # Pre-warmed next credential task

    # Internal function: quickly update credential (only updates token and project_id to avoid rebuilding entire request)
    async def refresh_credential_fast():
        nonlocal current_file, access_token, auth_headers, project_id, final_payload
        cred_result = await get_valid_credential(
            model_name=model_name, session_key=session_key
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        # Only update token and project_id, avoid rebuilding entire headers and payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, access_token, project_id, auth_headers, final_payload
        current_file, credential_data = cred_result
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    for attempt in range(max_retries + 1):
        need_retry = False  # Flag indicating whether retry is needed
        attempt_start_time = time.time()  # Start time of current upstream call
        
        try:
            response = await post_async(
                url=target_url,
                json=final_payload,
                headers=auth_headers
            )

            status_code = response.status_code

            # Success
            if status_code == 200:
                # Check if response is empty
                if not response.content or len(response.content) == 0:
                    log.warning(f"[ANTIGRAVITY] Received 200 response but content is empty, credential: {current_file}")
                    
                    # Record error
                    await record_api_call_error(current_file, 200, None, model_name=model_name,
                        error_message="Empty response from API"
                    )
                    # One log entry produced for this upstream call (empty response)
                    await _log_result(200, False, error_message="Empty response from API")
                    
                    if attempt < max_retries:
                        need_retry = True
                    else:
                        log.error(f"[ANTIGRAVITY] Empty response reached maximum retries")
                        return Response(
                            content=json.dumps({"error": "Upstream service returned empty response"}),
                            status_code=500,
                            media_type="application/json"
                        )
                else:
                    # Normal response
                    await record_api_call_success(current_file, model_name=model_name)
                    await _log_result(200, True, response_content=response.content)
                    return Response(
                        content=response.content,
                        status_code=200,
                        headers=dict(response.headers)
                    )

            # Failure - record last error
            if status_code != 200:
                last_error_response = Response(
                    content=response.content,
                    status_code=status_code,
                    headers=dict(response.headers)
                )

                # Determine whether retry is needed
                # Cache error text to avoid repeated parsing
                error_text = ""
                try:
                    error_text = response.text
                except Exception:
                    pass
                last_error_body = error_text

                if _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                    log.warning(f"[ANTIGRAVITY] Non-streaming request failed (status={status_code}), credential: {current_file}, response: {error_text[:500] if error_text else 'none'}")

                    # Parse cooldown time
                    cooldown_until = None
                    if (status_code == 429 or status_code == 503) and error_text:
                        try:
                            cooldown_until = await parse_and_log_cooldown(error_text)
                        except Exception:
                            pass

                    # Parallel pre-warm of next credential without blocking current handling
                    if next_cred_task is None and attempt < max_retries:
                        next_cred_task = asyncio.create_task(
                                get_valid_credential(
                                    model_name=model_name, session_key=session_key
                                )
                            )

                    # Record error and switch credential
                    await record_api_call_error(current_file, status_code, cooldown_until, model_name=model_name,
                        error_message=error_text
                    )
                    # One log entry produced for this upstream call (retries will record entries respectively)
                    await _log_result(status_code, False, error_message=error_text[:500] or f"HTTP {status_code}")

                    # Check whether to retry
                    should_retry = await handle_error_with_retry(status_code, current_file, retry_config["retry_enabled"], attempt, max_retries, retry_interval)

                    if should_retry and attempt < max_retries:
                        need_retry = True
                    else:
                        # Do not retry, directly return original error
                        log.error(f"[ANTIGRAVITY] Reached maximum retries or retry not eligible, returning original error")
                        return last_error_response
                else:
                    # Error code is not in disable codes, return directly without retry
                    log.error(f"[ANTIGRAVITY] Non-streaming request failed with non-retryable status (status={status_code}), credential: {current_file}, response: {error_text[:500] if error_text else 'none'}")
                    await record_api_call_error(current_file, status_code, None, model_name=model_name,
                        error_message=error_text
                    )
                    await _log_result(status_code, False, error_message=error_text[:500] or f"HTTP {status_code}")
                    return last_error_response
            
            # Unified retry handling
            if need_retry:
                log.info(f"[ANTIGRAVITY] Retrying request (attempt {attempt + 2}/{max_retries + 1})...")

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY]",
                )
                if not switched:
                    # Failed to switch credential, no Google API call made this time, no log generated
                    log.error("[ANTIGRAVITY] No available credentials or token on retry")
                    return Response(
                        content=json.dumps({"error": "No available credentials"}),
                        status_code=500,
                        media_type="application/json"
                    )
                continue  # Retry

        except Exception as e:
            log.error(f"[ANTIGRAVITY] Non-streaming request exception: {e}, credential: {current_file}")
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY] Retrying after exception (attempt {attempt + 2}/{max_retries + 1})...")
                # One log entry produced for this upstream call (exception)
                await _log_result(500, False, error_message=f"Exception: {e}")
                await asyncio.sleep(retry_interval)
                continue
            else:
                # All retries failed, return the last error (if any) or 500 error
                log.error(f"[ANTIGRAVITY] All retries failed, last exception: {e}")
                await _log_result(500, False, error_message=f"Exception: {e}")
                if last_error_response:
                    return last_error_response
                else:
                    return Response(
                        content=json.dumps({"error": f"Non-streaming request exception: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )

    # All retries failed (for loop ended normally) — each upstream call already recorded its log,
    # so return the last original error directly without extra logging
    log.error("[ANTIGRAVITY] All retries failed")
    if last_error_response:
        return last_error_response
    else:
        return Response(
            content=json.dumps({"error": "All retries failed"}),
            status_code=500,
            media_type="application/json"
        )


# ==================== Model and Quota Queries ====================

# Model list whitelist families (allowed if name contains any of these)
_MODEL_FAMILY_WHITELIST = ("claude", "gemini", "gpt", "argon")


def _is_listable_model(model_id: str) -> bool:
    """Filter models: drop disabled, keep claude, gpt, argon, and gemini >= 3.8 only"""
    lowered = (model_id or "").lower()
    if "disabled" in lowered:
        return False
    if "claude" in lowered or "gpt" in lowered or "argon" in lowered:
        return True
    if "gemini" in lowered:
        match = re.search(r"gemini-(\d+(?:\.\d+)?)", lowered)
        if match:
            try:
                return float(match.group(1)) >= 3.8
            except ValueError:
                pass
        return "3.8" in lowered
    return False


async def fetch_available_models() -> List[Dict[str, Any]]:
    """
    Get available model list, returning format compliant with OpenAI API specifications.
    Filtering rules: models containing 'disabled' in the name are excluded; models not
    containing claude/gemini/gpt/argon are excluded.

    Returns:
        List of models formatted as a list of dictionaries.

    Raises:
        Returns empty list if retrieval fails.
    """
    # Get valid credential
    cred_result = await get_valid_credential()
    if not cred_result:
        log.error("[ANTIGRAVITY] No valid credentials available for fetching models")
        return []

    current_file, credential_data = cred_result
    access_token = credential_data.get("access_token") or credential_data.get("token")

    if not access_token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {current_file}")
        return []

    # Build request headers
    headers = build_antigravity_headers(access_token, model_name="agent")

    try:
        # Use POST request to fetch model list
        response = await post_async(
            url=f"{await get_antigravity_api_url()}/v1internal:fetchAvailableModels",
            json={},  # Empty request body
            headers=headers
        )

        if response.status_code == 200:
            data = response.json()
            log.debug(f"[ANTIGRAVITY] Raw models response: {json.dumps(data, ensure_ascii=False)[:500]}")

            # Convert to OpenAI format model list using Model class
            model_list = []
            current_timestamp = int(datetime.now(timezone.utc).timestamp())

            if 'models' in data and isinstance(data['models'], dict):
                # Iterate model dict (apply filtering rules)
                for model_id in data['models'].keys():
                    if not _is_listable_model(model_id):
                        continue
                    model = Model(
                        id=model_id,
                        object='model',
                        created=current_timestamp,
                        owned_by='google'
                    )
                    model_list.append(model_to_dict(model))
            # Add extra claude-sonnet-4-6-thinking model
            if "claude-sonnet-4-6" in data.get('models', {}):
                model = Model(
                    id='claude-sonnet-4-6-thinking',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(model))
            # Add extra claude-opus-4-6 model
            if "claude-opus-4-6-thinking" in data.get('models', {}):
                claude_opus_model = Model(
                    id='claude-opus-4-6',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(claude_opus_model))

            log.info(f"[ANTIGRAVITY] Fetched {len(model_list)} available models")
            return model_list
        else:
            log.error(f"[ANTIGRAVITY] Failed to fetch models ({response.status_code}): {response.text[:500]}")
            return []

    except Exception as e:
        log.error(f"[ANTIGRAVITY] Failed to fetch models: {e}")
        log.error(f"[ANTIGRAVITY] Traceback: {traceback.format_exc()}")
        return []


async def fetch_quota_info(access_token: str, project_id: str = "") -> Dict[str, Any]:
    """
    Get quota information for the specified credential (uses retrieveUserQuotaSummary to obtain weekly+5h grouped quotas).

    Args:
        access_token: Antigravity access token
        project_id: Project ID corresponding to the credential

    Returns:
        Dictionary containing quota information in format:
        {
            "success": True/False,
            "models": {
                "bucket_id": {
                    "remaining": 0.95,
                    "resetTime": "12-20 10:30",
                    "resetTimeRaw": "2025-12-20T02:30:00Z",
                    "resetDays": 5,
                    "window": "weekly",
                    "group": "gemini 3 pro"
                }
            },
            "minResetDays": 5,
            "accountTier": "pro"/"normal",
            "isPro": True/False,
            "quotaGroups": [...],
            "source": "retrieveUserQuotaSummary",
            "error": "Error message" (only on failure)
        }
    """

    headers = build_antigravity_headers(access_token, model_name="agent")

    url = f"{await get_antigravity_api_url()}/v1internal:retrieveUserQuotaSummary"
    payload: Dict[str, Any] = {}
    if project_id:
        payload["project"] = project_id

    log.info(f"[ANTIGRAVITY QUOTA] fetch_quota_info: project={project_id}, url={url}")

    try:
        response = await post_async(
            url=url,
            json=payload,
            headers=headers,
            timeout=30.0
        )

        if response.status_code != 200:
            error_text = response.text[:500]
            log.error(f"[ANTIGRAVITY QUOTA] fetch_quota_info failed: {response.status_code} - {error_text}")
            return {"success": False, "error": f"API returned error: {response.status_code} - {error_text}"}

        data = response.json()
        log.debug(f"[ANTIGRAVITY QUOTA] Raw response: {json.dumps(data, ensure_ascii=False)[:500]}")

        # Parse grouped quotas (weekly + 5h dual windows)
        quota_groups: List[Dict[str, Any]] = []
        for g in data.get("groups", []):
            buckets = []
            for b in g.get("buckets", []):
                buckets.append({
                    "bucketId": b.get("bucketId", ""),
                    "displayName": b.get("displayName", ""),
                    "window": b.get("window", ""),
                    "resetTime": b.get("resetTime", ""),
                    "remainingFraction": b.get("remainingFraction", 0),
                    "description": b.get("description"),
                })
            quota_groups.append({
                "displayName": g.get("displayName", ""),
                "description": g.get("description"),
                "buckets": buckets,
            })

        # Infer account tier and reset days from grouped quotas
        min_reset_days = None
        for group in quota_groups:
            for bucket in group.get("buckets", []):
                reset_time = bucket.get("resetTime", "")
                if reset_time:
                    try:
                        reset_dt = datetime.fromisoformat(reset_time.replace('Z', '+00:00'))
                        now = datetime.now(timezone.utc)
                        delta = reset_dt - now
                        reset_days = max(0, delta.days)
                        if min_reset_days is None or reset_days < min_reset_days:
                            min_reset_days = reset_days
                    except Exception:
                        pass

        # Determine account tier: inferred from weekly window reset cycle
        is_pro = min_reset_days is not None and min_reset_days <= 1
        account_tier = "pro" if is_pro else "normal"

        # Build quota mapping (flattened from grouped data, backward compatible)
        quota_info: Dict[str, Dict[str, Any]] = {}
        for group in quota_groups:
            group_name = group.get("displayName", "").lower()
            for bucket in group.get("buckets", []):
                bucket_id = bucket.get("bucketId", "")
                remaining = bucket.get("remainingFraction", 0)
                reset_time = bucket.get("resetTime", "")
                window = bucket.get("window", "")

                # Convert to Beijing time (UTC+8)
                reset_time_beijing = 'N/A'
                reset_days = None
                if reset_time:
                    try:
                        utc_date = datetime.fromisoformat(reset_time.replace('Z', '+00:00'))
                        # Convert to Beijing time (UTC+8)
                        beijing_date = utc_date + timedelta(hours=8)
                        reset_time_beijing = beijing_date.strftime('%m-%d %H:%M')
                        now = datetime.now(timezone.utc)
                        delta = utc_date - now
                        reset_days = max(0, delta.days)
                    except Exception as e:
                        log.warning(f"[ANTIGRAVITY QUOTA] Failed to parse reset time: {e}")

                quota_info[bucket_id] = {
                    "remaining": remaining,
                    "resetTime": reset_time_beijing,
                    "resetTimeRaw": reset_time,
                    "resetDays": reset_days,
                    "window": window,
                    "group": group_name,
                }

        log.info(
            f"[ANTIGRAVITY QUOTA] Parsed {len(quota_groups)} groups, "
            f"{len(quota_info)} quota buckets, tier={account_tier}"
        )
        return {
            "success": True,
            "models": quota_info,
            "minResetDays": min_reset_days,
            "accountTier": account_tier,
            "isPro": is_pro,
            "quotaGroups": quota_groups,
            "source": "retrieveUserQuotaSummary",
        }
    except Exception as e:
        log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota: {e}")
        log.error(f"[ANTIGRAVITY QUOTA] Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e)
        }
