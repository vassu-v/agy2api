"""
OpenAI Router - Handles OpenAI format API requests via Antigravity
Router module for handling OpenAI format requests via Antigravity
"""

# Standard library
import json
import sys
import uuid
from pathlib import Path

# Add project root directory to Python path
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Third-party libraries
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# Local modules - Configuration and logging
from config import get_anti_truncation_max_attempts
from log import log

# Local modules - Utilities and authentication
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    authenticate_bearer,
)

# Local modules - Base router utilities
from src.router.hi_check import is_health_check_request, create_health_check_response
from src.router.stream_passthrough import (
    build_streaming_response_or_error,
    prepend_async_item,
    read_first_async_item,
)

# Local modules - Data models
from src.models import OpenAIChatCompletionRequest, model_to_dict

# Local modules - API layer
from src.api.antigravity import non_stream_request, stream_request

# Local modules - Converters
from src.converter.openai2gemini import (
    convert_openai_to_gemini_request,
    convert_gemini_to_openai_response,
    convert_gemini_to_openai_stream,
)
from src.converter.antigravity_fix import normalize_antigravity_request
from src.converter.anti_truncation import (
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
)


# ==================== Router Initialization ====================

router = APIRouter()


# ==================== API Routes ====================

@router.post("/v1/chat/completions")
async def chat_completions(
    openai_request: OpenAIChatCompletionRequest,
    request: Request,
    token: str = Depends(authenticate_bearer)
):
    """
    Handle OpenAI format chat completion requests (streaming and non-streaming)

    Args:
        openai_request: OpenAI format request body
        token: Bearer authentication token
    """
    log.debug(f"[ANTIGRAVITY-OPENAI] Request for model: {openai_request.model}")

    # Convert to dictionary
    normalized_dict = model_to_dict(openai_request)

    # Health check
    if is_health_check_request(normalized_dict, format="openai"):
        response = create_health_check_response(format="openai")
        return JSONResponse(content=response)

    # Process model name and feature detection
    use_anti_truncation = is_anti_truncation_model(openai_request.model)
    real_model = get_base_model_from_feature_model(openai_request.model)

    # Get streaming flag
    is_streaming = openai_request.stream

    # Warn for non-streaming requests with anti-truncation models
    if use_anti_truncation and not is_streaming:
        log.warning("Anti-truncation feature is only effective during streaming; non-streaming requests will ignore this setting")

    # Update model name to real model name
    normalized_dict["model"] = real_model

    # Convert to Gemini format (using converter)
    try:
        gemini_dict = await convert_openai_to_gemini_request(normalized_dict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # convert_openai_to_gemini_request does not include model field, need to add manually
    gemini_dict["model"] = real_model

    # Normalize Gemini request (using antigravity mode)
    gemini_dict = await normalize_antigravity_request(gemini_dict)

    # This endpoint accepts OpenAI roles, but the Antigravity content schema
    # accepts only USER/MODEL. Catch a conversion regression before HTTP send.
    for content in gemini_dict.get("contents", []):
        if not isinstance(content, dict) or content.get("role") not in ("user", "model"):
            raise HTTPException(
                status_code=400, detail="Unrepresentable content role for Antigravity"
            )

    # Prepare API request format - extract model and place other fields into request
    api_request = {
        "model": gemini_dict.pop("model"),
        "request": gemini_dict
    }

    # Request log context (api_format + client IP + original client model name)
    log_ctx = {
        "api_format": "openai",
        "client_ip": request.client.host if request.client else "",
        "client_model": openai_request.model,
    }

    # ========== Non-streaming Request ==========
    if not is_streaming:
        # Call API layer non-streaming request
        response = await non_stream_request(body=api_request, log_ctx=log_ctx)

        # Check response status code
        status_code = getattr(response, "status_code", 200)

        # Extract response body
        if hasattr(response, "body"):
            response_body = response.body.decode() if isinstance(response.body, bytes) else response.body
        elif hasattr(response, "content"):
            response_body = response.content.decode() if isinstance(response.content, bytes) else response.content
        else:
            response_body = str(response)

        try:
            gemini_response = json.loads(response_body)
        except Exception as e:
            log.error(f"Failed to parse Gemini response: {e}")
            raise HTTPException(status_code=500, detail="Response parsing failed")

        # Convert to OpenAI format
        openai_response = convert_gemini_to_openai_response(
            gemini_response,
            real_model,
            status_code
        )

        return JSONResponse(content=openai_response, status_code=status_code)

    # ========== Streaming Request ==========

    # ========== Streaming Anti-truncation Generator ==========
    async def anti_truncation_generator():
        max_attempts = await get_anti_truncation_max_attempts()

        # First apply anti-truncation instructions to payload
        anti_truncation_payload = apply_anti_truncation(api_request)

        first_attempt_stream = stream_request(body=anti_truncation_payload, native=False, log_ctx=log_ctx)
        try:
            first_chunk = await read_first_async_item(first_attempt_stream)
        except StopAsyncIteration:
            return

        if isinstance(first_chunk, Response):
            yield first_chunk
            return

        first_attempt_pending = True

        async def stream_request_wrapper(payload):
            nonlocal first_attempt_pending

            if first_attempt_pending:
                first_attempt_pending = False
                stream_gen = prepend_async_item(first_chunk, first_attempt_stream)
            else:
                stream_gen = stream_request(body=payload, native=False, log_ctx=log_ctx)

            return StreamingResponse(stream_gen, media_type="text/event-stream")

        # Create anti-truncation processor
        processor = AntiTruncationStreamProcessor(
            stream_request_wrapper,
            anti_truncation_payload,
            max_attempts,
        )

        # Convert to OpenAI format
        response_id = str(uuid.uuid4())

        # Directly iterate process_stream() generator and convert to OpenAI format
        async for chunk in processor.process_stream():
            if not chunk:
                continue

            # Parse Gemini SSE format
            chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

            # Skip empty lines
            if not chunk_str.strip():
                continue

            # Handle [DONE] marker
            if chunk_str.strip() == "data: [DONE]":
                yield "data: [DONE]\n\n".encode('utf-8')
                return

            # Parse "data: {...}" format
            if chunk_str.startswith("data: "):
                try:
                    # Convert to OpenAI format
                    openai_chunk_str = convert_gemini_to_openai_stream(
                        chunk_str,
                        real_model,
                        response_id
                    )

                    if openai_chunk_str:
                        yield openai_chunk_str.encode('utf-8')

                except Exception as e:
                    log.error(f"Failed to convert chunk: {e}")
                    continue

        # Send end marker
        yield "data: [DONE]\n\n".encode('utf-8')

    # ========== Standard Streaming Generator ==========
    async def normal_stream_generator():
        # Call API layer streaming request (native=False)
        stream_gen = stream_request(body=api_request, native=False, log_ctx=log_ctx)
        try:
            first_chunk = await read_first_async_item(stream_gen)
        except StopAsyncIteration:
            return

        if isinstance(first_chunk, Response):
            yield first_chunk
            return

        response_id = str(uuid.uuid4())

        # Yield all data, handle potential error Responses
        async for chunk in prepend_async_item(first_chunk, stream_gen):
            # Check if it is a Response object (error case)
            if isinstance(chunk, Response):
                # Convert Response to SSE formatted error message
                try:
                    error_content = chunk.body if isinstance(chunk.body, bytes) else (chunk.body or b'').encode('utf-8')
                    gemini_error = json.loads(error_content.decode('utf-8'))
                    # Convert to OpenAI format error
                    openai_error = convert_gemini_to_openai_response(
                        gemini_error,
                        real_model,
                        chunk.status_code
                    )
                    yield f"data: {json.dumps(openai_error)}\n\n".encode('utf-8')
                except Exception:
                    yield f"data: {json.dumps({'error': 'Stream error'})}\n\n".encode('utf-8')
                yield b"data: [DONE]\n\n"
                return
            else:
                # Normal bytes data, convert to OpenAI format
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

                # Skip empty lines
                if not chunk_str.strip():
                    continue

                # Handle [DONE] marker
                if chunk_str.strip() == "data: [DONE]":
                    yield "data: [DONE]\n\n".encode('utf-8')
                    return

                # Parse and convert Gemini chunk to OpenAI format
                if chunk_str.startswith("data: "):
                    try:
                        # Convert to OpenAI format
                        openai_chunk_str = convert_gemini_to_openai_stream(
                            chunk_str,
                            real_model,
                            response_id
                        )

                        if openai_chunk_str:
                            yield openai_chunk_str.encode('utf-8')

                    except Exception as e:
                        log.error(f"Failed to convert chunk: {e}")
                        continue

        # Send end marker
        yield "data: [DONE]\n\n".encode('utf-8')

    # ========== Select Generator Based on Mode ==========
    if use_anti_truncation:
        log.info("Enabling streaming anti-truncation feature")
        return await build_streaming_response_or_error(anti_truncation_generator())
    else:
        return await build_streaming_response_or_error(normal_stream_generator())
