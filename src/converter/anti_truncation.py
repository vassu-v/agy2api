"""
Anti-Truncation Module - Synthetic Tool Call Approach
By injecting a synthetic tool (emit_answer), the model puts its final answer into the content parameter of functionCall.
The gateway then extracts content from functionCall and restores it to standard assistant text, bypassing external moderation truncation.

Enforcement mechanism: toolConfig.functionCallingConfig.mode=ANY + allowedFunctionNames
Structurally requires the model to call one of the allowed tools (including emit_answer); the prompt only acts as an instruction for content quality.
When upstream ignores toolConfig, the streaming processor provides fallback (passes through plain text / continues generation).

Reference: https://github.com/Xeltra233/Antigravity-anti-truncation-gateway
"""

import io
import json
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi import Response as FastAPIResponse
from fastapi.responses import StreamingResponse

from src.converter.thoughtSignature_fix import SKIP_THOUGHT_SIGNATURE_VALIDATOR

# ==================== Synthetic Tool Configuration ====================

SYNTHETIC_TOOL_NAME = "emit_answer"

# finishReason considered as "output truncated" (triggers continuation even when synthetic tool matches);
# Other reasons like STOP / SAFETY are considered natural completion and do not trigger continuation
TRUNCATION_FINISH_REASONS = {"MAX_TOKENS"}

SYNTHETIC_TOOL_DECLARATION = {
    "name": SYNTHETIC_TOOL_NAME,
    "description": (
        "You MUST call this tool exactly once to output your final user-visible answer. "
        "Put the complete answer in the 'content' argument. "
        "Do NOT output any text outside this tool call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The complete final answer to output to the user.",
            },
        },
        "required": ["content"],
    },
}

CONTROL_INSTRUCTION = f"""Answer output rules (handled by the gateway, please organize your answer as follows):

1. Your final answer must be output by calling the `{SYNTHETIC_TOOL_NAME}` tool, placing the complete answer in the `content` argument.
2. The `content` parameter contains the complete natural language answer intended for the user (can include Markdown, code blocks, etc.); the user will only see the content of this parameter.
3. Do not output user-visible content in plain text outside the tool call.
4. If the answer is very long, you may output it across multiple `{SYNTHETIC_TOOL_NAME}` calls in chunks; do not omit content due to length.

If your answer is truncated, the system will ask you to continue outputting the remaining content, at which point you should continue outputting the remainder via `{SYNTHETIC_TOOL_NAME}`."""

CONTINUATION_PROMPT = f"""Your previous response was truncated. Please call the `{SYNTHETIC_TOOL_NAME}` tool to continue outputting all remaining content.

Important reminders:
1. Do not repeat content that was already output previously.
2. Continue outputting directly without any preamble or explanation.
3. Place the remaining content in the `content` argument of the `{SYNTHETIC_TOOL_NAME}` tool.

Now please continue outputting:"""



# ==================== Request Injection ====================


def _collect_function_tool_names(tools: List[Dict[str, Any]]) -> List[str]:
    """Collect tool names from all functionDeclarations in tools."""
    names: List[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        for decl in tool.get("functionDeclarations") or []:
            if isinstance(decl, dict) and decl.get("name"):
                names.append(decl["name"])
    return names


def _has_non_function_tool(tools: List[Dict[str, Any]]) -> bool:
    """Determine whether tools contain non-function tools that cannot be enumerated in allowedFunctionNames
    (built-in capabilities such as googleSearch / codeExecution / urlContext)."""
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("functionDeclarations"):
            continue
        # Tools without functionDeclarations are built-in capability tools
        return True
    return False


def _resolve_function_calling_mode(tools: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """Determine the injected functionCallingConfig.

    Defaults to injecting ANY + allowedFunctionNames (including emit_answer) for structural enforcement;
    when unenumerable non-function tools exist (e.g., googleSearch), allowedFunctionNames would
    block built-in capabilities, so it automatically falls back to AUTO.
    """
    if not _has_non_function_tool(tools):
        return "ANY", True
    return "AUTO", False


def apply_anti_truncation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply anti-truncation processing to request payload: inject synthetic tool and control instructions.

    Args:
        payload: Original request payload in format {"model": ..., "request": {...}}

    Returns:
        Payload injected with synthetic tool and control instructions.
    """
    modified_payload = payload.copy()
    # Copy request layer to prevent injection from mutating caller's original request body in-place
    # (under shallow copy, request_data shares the same dict with caller)
    request_data = dict(modified_payload.get("request") or {})

    # 1. Inject synthetic tool into tools list
    tools = list(request_data.get("tools") or [])
    # Check if already injected
    already_injected = any(
        isinstance(tool, dict)
        and any(
            decl.get("name") == SYNTHETIC_TOOL_NAME
            for decl in (tool.get("functionDeclarations") or [])
            if isinstance(decl, dict)
        )
        for tool in tools
    )
    if not already_injected:
        tools.append({"functionDeclarations": [SYNTHETIC_TOOL_DECLARATION]})
        request_data["tools"] = tools

    # 2. Inject toolConfig.functionCallingConfig to structurally force model to call emit_answer
    #    Deep copy toolConfig: prevent inner dict from sharing reference with caller and mutating in-place
    tool_config = json.loads(
        json.dumps(request_data.get("toolConfig") or {}, ensure_ascii=False)
    )
    func_config = dict(tool_config.get("functionCallingConfig") or {})
    mode, use_allowed = _resolve_function_calling_mode(tools)
    func_config["mode"] = mode
    if use_allowed:
        # Merge caller's existing allowedFunctionNames (if any), deduplicating while preserving order
        existing_allowed = [
            n for n in (func_config.get("allowedFunctionNames") or []) if isinstance(n, str)
        ]
        # tools already had emit_answer injected, collecting them naturally includes real tools + emit_answer
        allowed_names = _collect_function_tool_names(tools)
        merged = existing_allowed + [n for n in allowed_names if n not in existing_allowed]
        seen = set()
        merged = [n for n in merged if not (n in seen or seen.add(n))]
        func_config["allowedFunctionNames"] = merged
        tool_config["functionCallingConfig"] = func_config
        request_data["toolConfig"] = tool_config
        print(
            f"Anti-truncation: toolConfig mode=ANY allowedFunctionNames={merged}",
            flush=True,
        )
    else:
        # AUTO fallback: preserve caller's original config, only overwrite mode
        tool_config["functionCallingConfig"] = func_config
        request_data["toolConfig"] = tool_config
        print(
            "Anti-truncation: toolConfig mode=AUTO (non-function tools present), "
            "relying on instruction only",
            flush=True,
        )

    # 3. Inject control instructions into systemInstruction
    system_instruction = dict(request_data.get("systemInstruction") or {})
    if "parts" not in system_instruction:
        system_instruction["parts"] = []
    else:
        # parts list might come from caller's original object, copy as well to avoid in-place append
        system_instruction["parts"] = list(system_instruction["parts"])

    has_control_instruction = any(
        isinstance(part, dict) and SYNTHETIC_TOOL_NAME in part.get("text", "")
        for part in system_instruction["parts"]
    )
    if not has_control_instruction:
        system_instruction["parts"].append({"text": CONTROL_INSTRUCTION})
        request_data["systemInstruction"] = system_instruction

    modified_payload["request"] = request_data
    return modified_payload




# ==================== Response Extraction ====================


def extract_synthetic_content_from_response(
    data: Dict[str, Any],
) -> Tuple[str, bool]:
    """
    Extract synthetic tool content from Gemini response.

    Args:
        data: Gemini response data (may contain response wrapper layer)

    Returns:
        (synthetic_content, found_synthetic) tuple:
        - synthetic_content: Text content extracted from emit_answer tool
        - found_synthetic: Whether a synthetic tool call was found
    """
    # Unwrap response field
    if "response" in data:
        data = data["response"]

    synthetic_content = ""
    found_synthetic = False

    for candidate in data.get("candidates", []):
        content = candidate.get("content", {})
        parts = content.get("parts", [])
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "functionCall" in part:
                fc = part["functionCall"]
                if fc.get("name") == SYNTHETIC_TOOL_NAME:
                    found_synthetic = True
                    args = fc.get("args", {})
                    if isinstance(args, dict):
                        synthetic_content += args.get("content", "")
                    elif isinstance(args, str):
                        # Attempt to parse JSON string
                        extracted = _extract_content_from_json_str(args)
                        if extracted is not None:
                            synthetic_content += extracted

    return synthetic_content, found_synthetic


def _extract_content_from_json_str(args_str: str) -> Optional[str]:
    """
    Extract content field from JSON string (with regex fallback).

    Args:
        args_str: JSON string

    Returns:
        Extracted content or None
    """
    # First try standard JSON parsing
    try:
        parsed = json.loads(args_str)
        if isinstance(parsed, dict) and "content" in parsed:
            return str(parsed["content"])
    except (json.JSONDecodeError, TypeError):
        pass

    # Regex fallback: match "content": "..." pattern
    # Supports escaped characters
    match = re.search(
        r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"',
        args_str,
        re.DOTALL,
    )
    if match:
        raw_content = match.group(1)
        # Decode JSON escape sequence
        try:
            return json.loads(f'"{raw_content}"')
        except (json.JSONDecodeError, ValueError):
            # Manually handle common escapes
            return (
                raw_content.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )

    return None



def _chunk_has_real_function_call(data: Dict[str, Any]) -> bool:
    """Check if chunk carries a real (non-emit_answer) functionCall part."""
    if "response" in data:
        data = data["response"]
    for candidate in data.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            if (
                isinstance(part, dict)
                and "functionCall" in part
                and part["functionCall"].get("name") != SYNTHETIC_TOOL_NAME
            ):
                return True
    return False


def _replace_thought_signatures(data: Dict[str, Any]) -> bool:
    """Replace real thoughtSignature in chunk with skip validation placeholder before returning to client.

    Anti-truncation rewrites the conversation (converts synthetic tool to text, concatenates continuation history).
    Real signatures issued by Gemini will trigger upstream Corrupted thought signature on client next round-trip.
    Referring to fingerprint handling in openai2gemini, uniformly replace before returning with
    thoughtSignature_fix's SKIP_THOUGHT_SIGNATURE_VALIDATOR (official skip validation placeholder).

    Modifies data in-place; returns whether a replacement occurred (caller can pass through as-is if unreplaced).
    """
    if not isinstance(data, dict):
        return False
    inner = data.get("response") if "response" in data else data
    if not isinstance(inner, dict):
        return False

    replaced = False
    for candidate in inner.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            signature = part.get("thoughtSignature")
            if signature and signature != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
                part["thoughtSignature"] = SKIP_THOUGHT_SIGNATURE_VALIDATOR
                replaced = True
    return replaced



def build_text_chunk_from_synthetic(
    original_data: Dict[str, Any],
    synthetic_content: str,
) -> Dict[str, Any]:
    """
    Build plain text chunk from content extracted from synthetic tool.

    Args:
        original_data: Original Gemini chunk data
        synthetic_content: Text extracted from synthetic tool

    Returns:
        Modified chunk data with functionCall(emit_answer) replaced by text part
    """
    # Unwrap response field
    has_response_wrapper = "response" in original_data
    if has_response_wrapper:
        inner_data = original_data["response"]
    else:
        inner_data = original_data

    modified_inner = inner_data.copy()
    modified_candidates = []

    for candidate in inner_data.get("candidates", []):
        modified_candidate = candidate.copy()
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        new_parts = []
        for part in parts:
            if not isinstance(part, dict):
                new_parts.append(part)
                continue

            if "functionCall" in part:
                fc = part["functionCall"]
                if fc.get("name") == SYNTHETIC_TOOL_NAME:
                    # Replace with text part
                    if synthetic_content:
                        new_parts.append({"text": synthetic_content})
                    # If content is empty, skip this part
                    continue
                else:
                    # Real tool call, preserve
                    new_parts.append(part)
            else:
                new_parts.append(part)

        modified_content = content.copy()
        modified_content["parts"] = new_parts
        modified_candidate["content"] = modified_content

        modified_candidates.append(modified_candidate)

    modified_inner["candidates"] = modified_candidates

    if has_response_wrapper:
        result = original_data.copy()
        result["response"] = modified_inner
        return result
    return modified_inner




# ==================== Stream Processor ====================


def _deep_copy_anti_truncation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-structural deep copy of payload (request / contents / messages / tools / toolConfig layers).

    AntiTruncationStreamProcessor appends assistant history and continuation prompts to contents / messages
    during continuation, and apply_anti_truncation injects content into tools / systemInstruction.
    If these containers share references with caller, injection and append would mutate the original request
    in-place, leading to duplicated stacking on retry or multiple continuations.
    A JSON roundtrip deep copy is used here; payloads are all JSON-serializable requests with acceptable cost.
    """
    try:
        return json.loads(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return payload.copy()


class AntiTruncationStreamProcessor:
    """Anti-truncation stream processor - based on synthetic tool calls.

    When the model does not use the synthetic tool and outputs plain text directly, text chunks are passed
    through to the client in real-time (no longer buffering and waiting for the full stream), and finishReason
    is sent uniformly at the end of the stream to avoid waiting as much as possible.
    """

    def __init__(
        self,
        original_request_func,
        payload: Dict[str, Any],
        max_attempts: int = 3,
    ):
        self.original_request_func = original_request_func
        # Deep copy request/contents/messages: this class appends to them during continuation;
        # after isolation, the caller's original request body (which might be reused in retries) remains uncontaminated
        self.base_payload = _deep_copy_anti_truncation_payload(payload)
        self.max_attempts = max_attempts
        self.collected_content = io.StringIO()
        self.current_attempt = 0
        # Whether any text was passed through in real-time to the client during the entire request
        # (used in exception fallback to judge whether salvaged text has already been sent, avoiding resending)
        self.forwarded_any_text = False

    def _get_collected_text(self) -> str:
        """Get collected text content."""
        return self.collected_content.getvalue()

    def _append_content(self, content: str):
        """Append content to collector."""
        if content:
            self.collected_content.write(content)

    def _clear_content(self):
        """Clear collected content and free memory."""
        self.collected_content.close()
        self.collected_content = io.StringIO()

    async def process_stream(self) -> AsyncGenerator[bytes, None]:
        """Process streaming response, detecting and handling truncation."""

        while self.current_attempt < self.max_attempts:
            self.current_attempt += 1

            # Build current request payload
            current_payload = self._build_current_payload()

            # Per-stream round state: initialize outside try block since exception path on stream interrupt needs access
            found_synthetic = False
            # Collect plain text already forwarded to client in real-time (for continuation history / error salvaging)
            side_buffer = io.StringIO()
            last_finish_reason: Optional[str] = None
            # Whether finishReason has already been forwarded to client (avoid duplicate emission at stream end)
            finish_reason_forwarded = False

            try:
                response = await self.original_request_func(current_payload)

                if not isinstance(response, StreamingResponse):
                    # Non-streaming response (upstream error JSON, etc.), wrap into SSE format output
                    # (aggregation layer only recognizes data: lines; raw JSON causes loss of error info)
                    raw = await self._handle_non_streaming_response(response)
                    if isinstance(raw, bytes):
                        yield f"data: {raw.decode('utf-8', errors='ignore').strip()}\n\n".encode("utf-8")
                    else:
                        yield f"data: {str(raw).strip()}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return

                async for line in response.body_iterator:
                    if not line:
                        yield line
                        continue

                    # Handle upstream generator yielding Response object (error response)
                    if isinstance(line, FastAPIResponse):
                        print(
                            f"Anti-truncation: Received Response object from stream "
                            f"(status={line.status_code}), treating as error",
                            flush=True,
                        )
                        error_chunk = {
                            "error": {
                                "message": (
                                    line.body.decode("utf-8", errors="ignore")
                                    if hasattr(line, "body") and line.body
                                    else "Upstream error"
                                ),
                                "type": "api_error",
                                "code": line.status_code,
                            }
                        }
                        yield f"data: {json.dumps(error_chunk)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return

                    # Decode bytes to string
                    if isinstance(line, bytes):
                        line_str = line.decode("utf-8", errors="ignore").strip()
                    else:
                        line_str = str(line).strip()

                    if not line_str:
                        yield line
                        continue

                    # Handle SSE format data line
                    if line_str.startswith("data: "):
                        payload_str = line_str[6:]

                        # Check for [DONE] tag
                        if payload_str.strip() == "[DONE]":
                            if found_synthetic:
                                if (
                                    last_finish_reason in TRUNCATION_FINISH_REASONS
                                    and self.current_attempt < self.max_attempts
                                ):
                                    # synthetic content truncated: do not terminate stream, break to proceed
                                    # with unified stream-end continuation evaluation (side_buffer handled at stream end)
                                    break
                                print(
                                    f"Anti-truncation: outcome=synthetic_hit "
                                    f"(attempt {self.current_attempt}, "
                                    f"content length: {len(self._get_collected_text())}, "
                                    f"finishReason: {last_finish_reason})",
                                    flush=True,
                                )
                                side_buffer.close()
                                self._clear_content()
                                if not finish_reason_forwarded:
                                    # Stream did not finish naturally: emit finishReason for clean client termination
                                    yield self._build_finish_reason_chunk()
                                yield line
                                return
                            else:
                                print(
                                    f"Anti-truncation: outcome=no_synthetic_eot "
                                    f"(attempt {self.current_attempt}, "
                                    f"forwarded text length: {len(side_buffer.getvalue())}), "
                                    "preparing continuation",
                                    flush=True,
                                )
                                # Do not send [DONE], prepare continuation
                                break

                        # Try parsing JSON data
                        try:
                            data = json.loads(payload_str)
                        except (json.JSONDecodeError, ValueError):
                            yield line
                            continue

                        # Replace real thoughtSignature with skip validation placeholder before returning,
                        # recalculate line after replacement to ensure pass-through branches also carry sanitized data
                        if _replace_thought_signatures(data):
                            line = (
                                f"data: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n"
                            ).encode("utf-8")

                        # Extract synthetic tool content
                        synthetic_content, chunk_has_synthetic = (
                            extract_synthetic_content_from_response(data)
                        )

                        if chunk_has_synthetic:
                            found_synthetic = True
                            # Under ANY enforcement emit_answer is the sole output channel, MAX_TOKENS
                            # truncation happens inside emit_answer: record finishReason,
                            # stream end logic determines whether to treat as complete or continue
                            chunk_finish = self._get_finish_reason(data)
                            if chunk_finish:
                                last_finish_reason = chunk_finish
                            # In real-time pass-through mode, text in side_buffer was already sent to client.
                            # If model outputs plain text first and then switches to synthetic tool, treat as conflict:
                            # discard forwarded plain text and log warning (client will see synthetic tool content)
                            if side_buffer.getvalue():
                                print(
                                    "Anti-truncation: Model output plain text before "
                                    "synthetic tool call, synthetic content wins "
                                    "(forwarded text conflicts and is dropped)",
                                    flush=True,
                                )
                                side_buffer.close()
                                side_buffer = io.StringIO()

                            # Collect content for continuation
                            self._append_content(synthetic_content)

                            # Build chunk: strip truncation reason (MAX_TOKENS) before forwarding —
                            # client seeing MAX_TOKENS would judge stream truncated and abort early;
                            # whether to continue after truncation is decided by gateway, uniformly emitting STOP at end
                            is_truncated = chunk_finish in TRUNCATION_FINISH_REASONS
                            if is_truncated:
                                stripped_data = self._strip_finish_reason(data)
                                modified_data = build_text_chunk_from_synthetic(
                                    stripped_data, synthetic_content
                                )
                            else:
                                modified_data = build_text_chunk_from_synthetic(
                                    data, synthetic_content
                                )
                            json_str = json.dumps(
                                modified_data, separators=(",", ":"), ensure_ascii=False
                            )
                            yield f"data: {json_str}\n\n".encode("utf-8")

                            # Replaced chunk preserves original finishReason (if any),
                            # already forwarded with yield above, mark to avoid duplicate emission at completion
                            if chunk_finish and not is_truncated:
                                last_finish_reason = chunk_finish
                                finish_reason_forwarded = True

                        elif _chunk_has_real_function_call(data):
                            # Real tool call, pass through as-is
                            chunk_finish = self._get_finish_reason(data)
                            if chunk_finish:
                                last_finish_reason = chunk_finish
                                # As-is forwarded chunk includes finishReason
                                finish_reason_forwarded = True
                            yield line

                        else:
                            # Plain text chunk
                            if found_synthetic:
                                # Synthetic tool call already found: pass through control chunks with finishReason
                                # (Gemini clients rely on finishReason to determine normal stream end),
                                # but clear text within them to prevent duplicates; discard pure text chunks (prevent concatenation)
                                if self._has_finish_reason(data):
                                    last_finish_reason = self._get_finish_reason(data)
                                    finish_reason_forwarded = True
                                    stripped = self._strip_text_parts(data)
                                    json_str = json.dumps(
                                        stripped, separators=(",", ":"), ensure_ascii=False
                                    )
                                    yield f"data: {json_str}\n\n".encode("utf-8")
                                continue
                            else:
                                # Model did not use synthetic tool and output plain text directly:
                                # Pass through to client in real-time (avoid buffering and waiting for full stream)
                                text = self._extract_text_from_chunk(data)
                                if text:
                                    side_buffer.write(text)
                                    self.forwarded_any_text = True
                                    if self._has_finish_reason(data):
                                        # Text and finishReason in same chunk: pass through text stripped of
                                        # finishReason; completion signal is decided uniformly at stream end
                                        stripped = self._strip_finish_reason(data)
                                        json_str = json.dumps(
                                            stripped, separators=(",", ":"), ensure_ascii=False
                                        )
                                        yield f"data: {json_str}\n\n".encode("utf-8")
                                    else:
                                        # Pure text chunk: pass through as-is
                                        yield line
                                chunk_finish = self._get_finish_reason(data)
                                if chunk_finish:
                                    last_finish_reason = chunk_finish
                                    # Hold completion control signal temporarily; decide continuation or emission after stream ends
                                    continue
                                if not text:
                                    # Empty chunk without text or tool calls: pass through (usage, etc.)
                                    yield line
                                continue

                    else:
                        # Non-data: line, pass through directly
                        yield line

                # Stream finished (break or normal completion)
                side_text = side_buffer.getvalue()
                side_buffer.close()

                if found_synthetic:
                    if last_finish_reason in TRUNCATION_FINISH_REASONS and self.current_attempt < self.max_attempts:
                        # Truncation under ANY enforcement occurred inside emit_answer:
                        # synthetic content is incomplete, continue to supplement
                        print(
                            f"Anti-truncation: outcome=synthetic_truncated "
                            f"(attempt {self.current_attempt} -> {self.current_attempt + 1}, "
                            f"content length: {len(self._get_collected_text())}, "
                            f"finishReason: {last_finish_reason})",
                            flush=True,
                        )
                        continue
                    content_len = len(self._get_collected_text())
                    self._clear_content()
                    print(
                        f"Anti-truncation: outcome=synthetic_hit "
                        f"(attempt {self.current_attempt}, stream ended without [DONE], "
                        f"content length: {content_len}, "
                        f"finishReason: {last_finish_reason})",
                        flush=True,
                    )
                    if not finish_reason_forwarded:
                        # Stream did not finish naturally (no finishReason chunk): send completion signal
                        yield self._build_finish_reason_chunk()
                    yield b"data: [DONE]\n\n"
                    return

                # Model ended naturally (STOP) without calling synthetic tool: treat as complete answer, do not continue
                # (continuation would cause model to re-output existing content, wasting upstream requests and risking duplicates)
                if last_finish_reason == "STOP":
                    print(
                        f"Anti-truncation: outcome=stop_plain_text "
                        f"(attempt {self.current_attempt}, "
                        f"text length: {len(side_text)}, finishReason=STOP "
                        "without synthetic tool call), treating as complete",
                        flush=True)
                    # Text was already forwarded in real-time; only need to emit withheld finishReason completion chunk
                    if not finish_reason_forwarded:
                        yield self._build_finish_reason_chunk(last_finish_reason)
                    self._clear_content()
                    yield b"data: [DONE]\n\n"
                    return

                # Synthetic tool call not received
                if side_text:
                    # Plain text present: already forwarded to client in real-time, no need to re-send
                    self._append_content(side_text)

                # Trigger continuation
                if self.current_attempt < self.max_attempts:
                    accumulated_text = self._get_collected_text()
                    print(
                        f"Anti-truncation: outcome=continuation "
                        f"(attempt {self.current_attempt} -> {self.current_attempt + 1}, "
                        f"accumulated length: {len(accumulated_text)}, "
                        f"last finishReason: {last_finish_reason})",
                        flush=True,
                    )
                    continue
                else:
                    print(
                        f"Anti-truncation: outcome=max_attempts_reached "
                        f"(attempt {self.current_attempt}, "
                        f"forwarded text length: {len(side_text)})",
                        flush=True,
                    )
                    # Reached retry limit: emit finishReason completion to let client end normally
                    if side_text and not finish_reason_forwarded:
                        yield self._build_finish_reason_chunk(last_finish_reason or "STOP")
                    self._clear_content()
                    yield b"data: [DONE]\n\n"
                    return

            except Exception as e:
                print(
                    f"Anti-truncation: outcome=error "
                    f"(attempt {self.current_attempt}): {str(e)}",
                    flush=True,
                )

                # Abnormal interruption: merge text received this round into collector as continuation history
                # (otherwise continuation request won't know where model left off, risking repetition from start)
                try:
                    interrupted_text = side_buffer.getvalue()
                except ValueError:
                    # Normal path already closed before error was raised: text was already merged into collector
                    interrupted_text = ""
                try:
                    side_buffer.close()
                except ValueError:
                    pass
                if interrupted_text:
                    print(
                        f"Anti-truncation: Stream interrupted, salvaging "
                        f"{len(interrupted_text)} chars of buffered text into continuation history",
                        flush=True,
                    )
                    self._append_content(interrupted_text)

                if self.current_attempt >= self.max_attempts:
                    # Retry quota exhausted: text already forwarded in real-time is not repeated, only send completion signal
                    salvaged = self._get_collected_text()
                    self._clear_content()
                    if salvaged and self.forwarded_any_text:
                        print(
                            f"Anti-truncation: Max attempts reached after error, "
                            f"salvaged text already forwarded (length: {len(salvaged)})",
                            flush=True,
                        )
                        if not finish_reason_forwarded:
                            yield self._build_finish_reason_chunk(last_finish_reason or "STOP")
                    elif salvaged:
                        # Content never forwarded (e.g. collected via synthetic tool): output as fallback
                        print(
                            f"Anti-truncation: Max attempts reached after error, "
                            f"yielding salvaged text (length: {len(salvaged)})",
                            flush=True,
                        )
                        fallback_chunk = self._build_fallback_text_chunk(salvaged)
                        if fallback_chunk:
                            yield fallback_chunk
                    else:
                        error_chunk = {
                            "error": {
                                "message": f"Anti-truncation failed: {str(e)}",
                                "type": "api_error",
                                "code": 500,
                            }
                        }
                        yield f"data: {json.dumps(error_chunk)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return
                # Retry quota remaining: proceed to next round of continuation
                continue

        # All attempts failed
        print("Anti-truncation: outcome=all_attempts_failed", flush=True)
        self._clear_content()
        yield b"data: [DONE]\n\n"

    def _build_current_payload(self) -> Dict[str, Any]:
        """Build payload for current request."""
        if self.current_attempt == 1:
            return self.base_payload

        # Subsequent requests: add continuation instructions.
        # Deep copy request layer and contents: model history / user continuation prompt appended
        # during continuation should not be written back to self.base_payload; otherwise subsequent
        # continuations stack repeatedly on top of old continuation content, causing request bloat
        # (multiple copies of full assistant text + multiple continuation prompts in history).
        continuation_payload = dict(self.base_payload)
        request_data = dict(continuation_payload.get("request") or {})
        new_contents = [
            dict(c) if isinstance(c, dict) else c
            for c in (request_data.get("contents") or [])
        ]

        # If there is collected content, add it to conversation
        accumulated_text = self._get_collected_text()
        if accumulated_text:
            new_contents.append({"role": "model", "parts": [{"text": accumulated_text}]})

        # Build continuation instruction
        content_summary = ""
        if accumulated_text:
            if len(accumulated_text) > 200:
                content_summary = (
                    f"\n\nPreviously, you already output approximately {len(accumulated_text)} characters of content, "
                    f'ending with:\n"...{accumulated_text[-100:]}"'
                )
            else:
                content_summary = f'\n\nPreviously, the content you output was:\n"{accumulated_text}"'

        detailed_continuation_prompt = f"{CONTINUATION_PROMPT}{content_summary}"

        continuation_message = {"role": "user", "parts": [{"text": detailed_continuation_prompt}]}
        new_contents.append(continuation_message)

        request_data["contents"] = new_contents
        continuation_payload["request"] = request_data

        return continuation_payload

    def _extract_text_from_chunk(self, data: Dict[str, Any]) -> str:
        """Extract plain text content from chunk data."""
        if "response" in data:
            data = data["response"]

        text = ""
        for candidate in data.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if isinstance(part, dict) and "text" in part:
                    text += part["text"]
        return text

    @staticmethod
    def _has_finish_reason(data: Dict[str, Any]) -> bool:
        """Determine whether chunk carries finishReason (control signal chunk)."""
        if "response" in data:
            data = data["response"]
        for candidate in data.get("candidates", []):
            if candidate.get("finishReason"):
                return True
        return False

    @staticmethod
    def _get_finish_reason(data: Dict[str, Any]) -> Optional[str]:
        """Extract finishReason value from chunk (or None)."""
        if "response" in data:
            data = data["response"]
        for candidate in data.get("candidates", []):
            reason = candidate.get("finishReason")
            if reason:
                return reason
        return None

    @staticmethod
    def _strip_text_parts(data: Dict[str, Any]) -> Dict[str, Any]:
        """Remove text content from all text parts in chunk (retaining control fields such as finishReason).

        Used when passing through finishReason chunk after found_synthetic to prevent duplicate text output.
        """
        has_wrapper = "response" in data
        inner = data["response"] if has_wrapper else data

        modified_inner = inner.copy()
        modified_candidates = []
        for candidate in inner.get("candidates", []):
            modified_candidate = candidate.copy()
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            # Filter out all text parts (preserving functionCall and other parts, though usually empty here)
            new_parts = [
                part for part in parts
                if not (isinstance(part, dict) and "text" in part)
            ]
            modified_content = content.copy()
            modified_content["parts"] = new_parts
            modified_candidate["content"] = modified_content
            modified_candidates.append(modified_candidate)

        modified_inner["candidates"] = modified_candidates
        if has_wrapper:
            result = data.copy()
            result["response"] = modified_inner
            return result
        return modified_inner

    @staticmethod
    def _strip_finish_reason(data: Dict[str, Any]) -> Dict[str, Any]:
        """Remove finishReason from all candidates in chunk (retaining text parts for pass-through)."""
        has_wrapper = "response" in data
        inner = data["response"] if has_wrapper else data

        modified_inner = inner.copy()
        modified_candidates = []
        for candidate in inner.get("candidates", []):
            if not isinstance(candidate, dict) or "finishReason" not in candidate:
                modified_candidates.append(candidate)
                continue
            modified_candidate = candidate.copy()
            modified_candidate.pop("finishReason", None)
            modified_candidates.append(modified_candidate)

        modified_inner["candidates"] = modified_candidates
        if has_wrapper:
            result = data.copy()
            result["response"] = modified_inner
            return result
        return modified_inner

    def _build_fallback_text_chunk(self, text: str) -> Optional[bytes]:
        """Build fallback text chunk (outputs buffered plain text when no synthetic tool call occurred)."""
        if not text:
            return None

        chunk = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "index": 0,
                }
            ]
        }
        json_str = json.dumps(chunk, separators=(",", ":"), ensure_ascii=False)
        return f"data: {json_str}\n\n".encode("utf-8")

    def _build_finish_reason_chunk(self, finish_reason: str = "STOP") -> bytes:
        """Build completion chunk containing only finishReason (text stripped to prevent duplicates)."""
        chunk = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": []},
                    "finishReason": finish_reason,
                    "index": 0,
                }
            ]
        }
        json_str = json.dumps(chunk, separators=(",", ":"), ensure_ascii=False)
        return f"data: {json_str}\n\n".encode("utf-8")

    async def _handle_non_streaming_response(self, response) -> bytes:
        """Handle non-streaming response."""
        while True:
            try:
                # Special handling: if returning StreamingResponse
                if isinstance(response, StreamingResponse):
                    print(
                        "Anti-truncation: Received StreamingResponse in non-streaming handler",
                        flush=True,
                    )
                    chunks = []
                    async for chunk in response.body_iterator:
                        chunks.append(chunk)
                    content = b"".join(chunks).decode() if chunks else ""
                elif hasattr(response, "body"):
                    content = (
                        response.body.decode()
                        if isinstance(response.body, bytes)
                        else response.body
                    )
                elif hasattr(response, "content"):
                    content = (
                        response.content.decode()
                        if isinstance(response.content, bytes)
                        else response.content
                    )
                else:
                    print(f"Anti-truncation: Unknown response type: {type(response)}", flush=True)
                    content = str(response)

                if not content or not content.strip():
                    print("Anti-truncation: Received empty response content", flush=True)
                    return json.dumps(
                        {
                            "error": {
                                "message": "Empty response from server",
                                "type": "api_error",
                                "code": 500,
                            }
                        }
                    ).encode()

                try:
                    response_data = json.loads(content)
                except json.JSONDecodeError as json_err:
                    print(
                        f"Anti-truncation: Failed to parse JSON response: {json_err}, "
                        f"content: {content[:200]}",
                        flush=True,
                    )
                    return content.encode() if isinstance(content, str) else content

                # Upstream error response: directly pass through error, do not continue
                if isinstance(response_data, dict) and "error" in response_data:
                    print(
                        f"Anti-truncation: Upstream error response "
                        f"(status={getattr(response, 'status_code', 'unknown')})",
                        flush=True,
                    )
                    return content.encode() if isinstance(content, str) else content

                # Extract synthetic tool content
                synthetic_content, found_synthetic = (
                    extract_synthetic_content_from_response(response_data)
                )

                if found_synthetic or self.current_attempt >= self.max_attempts:
                    if found_synthetic:
                        # Replace synthetic tool call in response with plain text
                        modified_data = build_text_chunk_from_synthetic(
                            response_data, synthetic_content
                        )
                        # Replace real thoughtSignature with skip validation placeholder before returning
                        _replace_thought_signatures(modified_data)
                        return json.dumps(modified_data, ensure_ascii=False).encode()
                    if isinstance(response_data, dict) and _replace_thought_signatures(
                        response_data
                    ):
                        # Replace real thoughtSignature with skip validation placeholder before returning
                        return json.dumps(response_data, ensure_ascii=False).encode()
                    return content.encode() if isinstance(content, str) else content

                # Continuation required
                if synthetic_content:
                    self._append_content(synthetic_content)
                else:
                    # Attempt to extract plain text
                    text = self._extract_text_from_chunk(response_data)
                    if text:
                        self._append_content(text)

                print("Anti-truncation: Non-streaming response needs continuation", flush=True)
                self.current_attempt += 1
                next_payload = self._build_current_payload()
                response = await self.original_request_func(next_payload)

            except Exception as e:
                print(f"Anti-truncation non-streaming error: {str(e)}", flush=True)
                return json.dumps(
                    {
                        "error": {
                            "message": f"Anti-truncation failed: {str(e)}",
                            "type": "api_error",
                            "code": 500,
                        }
                    }
                ).encode()


