"""
Gemini Router - Handles native Gemini format API requests (Antigravity backend)
"""

# Standard library
import json
import sys
import traceback
from pathlib import Path as _PathlibPath

# Add project root directory to Python path
project_root = _PathlibPath(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Third-party libraries
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

# Local modules - Configuration and logging
from config import get_anti_truncation_max_attempts
from log import log

# Local modules - Utilities and authentication
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    authenticate_gemini_flexible,
    check_model_allowed,
)

# Local modules - Base router utilities
from src.router.hi_check import is_health_check_request, create_health_check_response
from src.router.stream_passthrough import (
    build_streaming_response_or_error,
    prepend_async_item,
    read_first_async_item,
)

# Local modules - Data models
from src.models import GeminiRequest, model_to_dict

# Local modules - API layer
from src.api.antigravity import non_stream_request, stream_request

# Local modules - Converters
from src.converter.antigravity_fix import normalize_antigravity_request
from src.converter.anti_truncation import (
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
)


# ==================== Router Initialization ====================

router = APIRouter()


# ==================== API Routes ====================

@router.post("/v1beta/models/{model:path}:generateContent")
@router.post("/v1/models/{model:path}:generateContent")
async def generate_content(
    gemini_request: "GeminiRequest",
    request: Request,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    Handle Gemini format content generation requests (non-streaming)

    Args:
        gemini_request: Gemini format request body
        model: Model name
        api_key: API key
    """
    log.debug(f"[ANTIGRAVITY] Non-streaming request for model: {model}")

    # Convert to dictionary
    normalized_dict = model_to_dict(gemini_request)

    # Health check
    if is_health_check_request(normalized_dict, format="gemini"):
        response = create_health_check_response(format="gemini")
        return JSONResponse(content=response)

    # Verify model permission for authenticated API key
    check_model_allowed(request, model)

    # Process model name and feature detection
    use_anti_truncation = is_anti_truncation_model(model)
    real_model = get_base_model_from_feature_model(model)

    # Warn for non-streaming requests with anti-truncation models
    if use_anti_truncation:
        log.warning("Anti-truncation feature is only effective during streaming; non-streaming requests will ignore this setting")

    # Update model name to real model name
    normalized_dict["model"] = real_model

    # Normalize Gemini request (using antigravity mode)
    normalized_dict = await normalize_antigravity_request(normalized_dict)

    # Prepare API request format - extract model and place other fields into request
    api_request = {
        "model": normalized_dict.pop("model"),
        "request": normalized_dict
    }

    api_key_meta = getattr(request.state, "api_key", {}) if hasattr(request, "state") else {}
    # Call API layer non-streaming request
    response = await non_stream_request(
        body=api_request,
        log_ctx={
            "api_format": "gemini",
            "client_ip": request.client.host if request.client else "",
            "client_model": model,
            "api_key_name": api_key_meta.get("name", ""),
            "api_key_hash": api_key_meta.get("key_hash", ""),
        },
    )

    # Unwrap response: Antigravity API may return a response with an extra wrapper layer
    # Extract and return standard Gemini format
    # Keep Gemini native inlineData format without Markdown conversion
    try:
        if response.status_code == 200:
            response_data = json.loads(response.body if hasattr(response, 'body') else response.content)
            # If response wrapper exists, unwrap it
            if "response" in response_data:
                unwrapped_data = response_data["response"]
                return JSONResponse(content=unwrapped_data)
        # Error response or missing response field, return directly
        return response
    except Exception as e:
        log.warning(f"Failed to unwrap response: {e}, returning original response")
        return response

@router.post("/v1beta/models/{model:path}:streamGenerateContent")
@router.post("/v1/models/{model:path}:streamGenerateContent")
async def stream_generate_content(
    gemini_request: GeminiRequest,
    request: Request,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    Handle Gemini format streaming content generation requests

    Args:
        gemini_request: Gemini format request body
        model: Model name
        api_key: API key
    """
    log.debug(f"[ANTIGRAVITY] Streaming request for model: {model}")

    # Convert to dictionary
    normalized_dict = model_to_dict(gemini_request)

    # Verify model permission for authenticated API key
    check_model_allowed(request, model)

    # Process model name and feature detection
    use_anti_truncation = is_anti_truncation_model(model)
    real_model = get_base_model_from_feature_model(model)

    # Update model name to real model name
    normalized_dict["model"] = real_model

    # Request log context (api_format + client IP + original client model name + API key metadata)
    api_key_meta = getattr(request.state, "api_key", {}) if hasattr(request, "state") else {}
    log_ctx = {
        "api_format": "gemini",
        "client_ip": request.client.host if request.client else "",
        "client_model": model,
        "api_key_name": api_key_meta.get("name", ""),
        "api_key_hash": api_key_meta.get("key_hash", ""),
    }

    # ========== Streaming Anti-truncation Generator ==========
    async def anti_truncation_generator():
        # Perform basic normalization first
        normalized_req = await normalize_antigravity_request(normalized_dict.copy())

        # Prepare API request format - extract model and place other fields into request
        api_request = {
            "model": normalized_req.pop("model") if "model" in normalized_req else real_model,
            "request": normalized_req
        }

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

        # Iterate process_stream() generator and unwrap response layer
        async for chunk in processor.process_stream():
            if isinstance(chunk, (str, bytes)):
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

                # Parse and unwrap response layer
                if chunk_str.startswith("data: "):
                    json_str = chunk_str[6:].strip()

                    # Skip [DONE] marker
                    if json_str == "[DONE]":
                        yield chunk
                        continue

                    try:
                        # Parse JSON
                        data = json.loads(json_str)

                        # Unwrap response layer
                        if "response" in data and "candidates" not in data:
                            log.debug(f"[ANTIGRAVITY-ANTI-TRUNCATION] Unwrapping response wrapper")
                            unwrapped_data = data["response"]
                            # Rebuild SSE format
                            yield f"data: {json.dumps(unwrapped_data, ensure_ascii=False)}\n\n".encode('utf-8')
                        else:
                            # Already unwrapped format, return directly
                            yield chunk
                    except json.JSONDecodeError:
                        # JSON parsing failed, return raw chunk directly
                        yield chunk
                else:
                    # Not SSE format, return directly
                    yield chunk
            else:
                # Other types, return directly
                yield chunk

    # ========== Standard Streaming Generator ==========
    async def normal_stream_generator():
        normalized_req = await normalize_antigravity_request(normalized_dict.copy())

        # Prepare API request format - extract model and place other fields into request
        api_request = {
            "model": normalized_req.pop("model"),
            "request": normalized_req
        }

        # All streaming requests use non-native mode (SSE format) and unwrap response layer
        log.debug(f"[ANTIGRAVITY] Using non-native mode, unwrapping response wrapper")
        stream_gen = stream_request(body=api_request, native=False, log_ctx=log_ctx)
        try:
            first_chunk = await read_first_async_item(stream_gen)
        except StopAsyncIteration:
            return

        if isinstance(first_chunk, Response):
            yield first_chunk
            return

        # Unwrap response layer
        async for chunk in prepend_async_item(first_chunk, stream_gen):
            # Check if it is a Response object (error case)
            if isinstance(chunk, Response):
                # Convert Response to SSE formatted error message
                try:
                    error_content = chunk.body if isinstance(chunk.body, bytes) else (chunk.body or b'').encode('utf-8')
                    error_json = json.loads(error_content.decode('utf-8'))
                except Exception:
                    error_json = {"error": {"code": chunk.status_code, "message": "upstream error", "status": "ERROR"}}
                log.error(f"[ANTIGRAVITY STREAM] Returning error to client: status={chunk.status_code}, error={str(error_json)[:200]}")
                yield f"data: {json.dumps(error_json)}\n\n".encode('utf-8')
                yield b"data: [DONE]\n\n"
                return

            # Process SSE format chunk
            if isinstance(chunk, (str, bytes)):
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

                # Parse and unwrap response layer
                if chunk_str.startswith("data: "):
                    json_str = chunk_str[6:].strip()

                    # Skip [DONE] marker
                    if json_str == "[DONE]":
                        yield chunk
                        continue

                    try:
                        # Parse JSON
                        data = json.loads(json_str)

                        # Unwrap response layer
                        if "response" in data and "candidates" not in data:
                            log.debug(f"[ANTIGRAVITY] Unwrapping response wrapper")
                            unwrapped_data = data["response"]
                            # Rebuild SSE format
                            yield f"data: {json.dumps(unwrapped_data, ensure_ascii=False)}\n\n".encode('utf-8')
                        else:
                            # Already unwrapped format, return directly
                            yield chunk
                    except json.JSONDecodeError:
                        # JSON parsing failed, return raw chunk directly
                        yield chunk
                else:
                    # Not SSE format, return directly
                    yield chunk

    # ========== Select Generator Based on Mode ==========
    if use_anti_truncation:
        log.info("Enabling streaming anti-truncation feature")
        return await build_streaming_response_or_error(anti_truncation_generator())
    else:
        return await build_streaming_response_or_error(normal_stream_generator())

@router.post("/v1beta/models/{model:path}:countTokens")
@router.post("/v1/models/{model:path}:countTokens")
async def count_tokens(
    request: Request = None,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    Simulate Gemini format token count

    Uses simple heuristic: approximately 4 characters = 1 token
    """
    if model:
        check_model_allowed(request, model)

    try:
        request_data = await request.json()
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    # Simple token count simulation - estimate based on text length
    total_tokens = 0

    # If contents field exists
    if "contents" in request_data:
        for content in request_data["contents"]:
            if "parts" in content:
                for part in content["parts"]:
                    if "text" in part:
                        # Simple estimation: approx 4 characters = 1 token
                        text_length = len(part["text"])
                        total_tokens += max(1, text_length // 4)

    # If generateContentRequest field exists
    elif "generateContentRequest" in request_data:
        gen_request = request_data["generateContentRequest"]
        if "contents" in gen_request:
            for content in gen_request["contents"]:
                if "parts" in content:
                    for part in content["parts"]:
                        if "text" in part:
                            text_length = len(part["text"])
                            total_tokens += max(1, text_length // 4)

    # Return Gemini format response
    return JSONResponse(content={"totalTokens": total_tokens})

# ==================== Test Code ====================

if __name__ == "__main__":
    """
    Test code: Demonstrate streaming and non-streaming responses of Gemini router
    Run: python src/router/antigravity/gemini.py
    """

    print("=" * 80)
    print("Gemini Router (Antigravity Backend) Test")
    print("=" * 80)

    # Create test app
    app = FastAPI()
    app.include_router(router)

    # Test client
    client = TestClient(app)

    # Test request body (Gemini format)
    test_request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "Hello, tell me a joke in one sentence."}]
            }
        ]
    }

    # Test API key (mock)
    test_api_key = "pwd"

    def test_non_stream_request():
        """Test non-streaming request"""
        print("\n" + "=" * 80)
        print("[Test 2] Non-streaming request (POST /v1/models/gemini-2.5-flash:generateContent)")
        print("=" * 80)
        print(f"Request body: {json.dumps(test_request_body, indent=2, ensure_ascii=False)}\n")

        response = client.post(
            "/v1/models/gemini-2.5-flash:generateContent",
            json=test_request_body,
            params={"key": test_api_key}
        )

        print("Non-streaming response data:")
        print("-" * 80)
        print(f"Status code: {response.status_code}")
        print(f"Content-Type: {response.headers.get('content-type', 'N/A')}")

        try:
            content = response.text
            print(f"\nResponse content (raw):\n{content}\n")

            # Try to parse JSON
            try:
                json_data = response.json()
                print(f"Response content (formatted JSON):")
                print(json.dumps(json_data, indent=2, ensure_ascii=False))
            except json.JSONDecodeError:
                print("(Non-JSON format)")
        except Exception as e:
            print(f"Content parsing failed: {e}")

    def test_stream_request():
        """Test streaming request"""
        print("\n" + "=" * 80)
        print("[Test 3] Streaming request (POST /v1/models/gemini-2.5-flash:streamGenerateContent)")
        print("=" * 80)
        print(f"Request body: {json.dumps(test_request_body, indent=2, ensure_ascii=False)}\n")

        print("Streaming response data (per chunk):")
        print("-" * 80)

        with client.stream(
            "POST",
            "/v1/models/gemini-2.5-flash:streamGenerateContent",
            json=test_request_body,
            params={"key": test_api_key}
        ) as response:
            print(f"Status code: {response.status_code}")
            print(f"Content-Type: {response.headers.get('content-type', 'N/A')}\n")

            chunk_count = 0
            for chunk in response.iter_bytes():
                if chunk:
                    chunk_count += 1
                    print(f"\nChunk #{chunk_count}:")
                    print(f"  Type: {type(chunk).__name__}")
                    print(f"  Length: {len(chunk)}")

                    # Decode chunk
                    try:
                        chunk_str = chunk.decode('utf-8')
                        print(f"  Content preview: {repr(chunk_str[:200] if len(chunk_str) > 200 else chunk_str)}")

                        # If SSE format, try to parse each line
                        if chunk_str.startswith("data: "):
                            # Split by lines, process each SSE event
                            for line in chunk_str.strip().split('\n'):
                                line = line.strip()
                                if not line:
                                    continue

                                if line == "data: [DONE]":
                                    print(f"  => Stream end marker")
                                elif line.startswith("data: "):
                                    try:
                                        json_str = line[6:]  # Strip "data: " prefix
                                        json_data = json.loads(json_str)
                                        print(f"  Parsed JSON: {json.dumps(json_data, indent=4, ensure_ascii=False)}")
                                    except Exception as e:
                                        print(f"  SSE parsing failed: {e}")
                    except Exception as e:
                        print(f"  Decoding failed: {e}")

            print(f"\nReceived a total of {chunk_count} chunks")

    def test_anti_truncation_stream_request():
        """Test streaming anti-truncation request"""
        print("\n" + "=" * 80)
        print("[Test 5] Streaming anti-truncation request (POST /v1/models/autostitch-gemini-2.5-flash:streamGenerateContent)")
        print("=" * 80)
        print(f"Request body: {json.dumps(test_request_body, indent=2, ensure_ascii=False)}\n")

        print("Streaming anti-truncation response data (per chunk):")
        print("-" * 80)

        with client.stream(
            "POST",
            "/v1/models/autostitch-gemini-2.5-flash:streamGenerateContent",
            json=test_request_body,
            params={"key": test_api_key}
        ) as response:
            print(f"Status code: {response.status_code}")
            print(f"Content-Type: {response.headers.get('content-type', 'N/A')}\n")

            chunk_count = 0
            for chunk in response.iter_bytes():
                if chunk:
                    chunk_count += 1
                    print(f"\nChunk #{chunk_count}:")
                    print(f"  Type: {type(chunk).__name__}")
                    print(f"  Length: {len(chunk)}")

                    # Decode chunk
                    try:
                        chunk_str = chunk.decode('utf-8')
                        print(f"  Content preview: {repr(chunk_str[:200] if len(chunk_str) > 200 else chunk_str)}")

                        # If SSE format, try to parse each line
                        if chunk_str.startswith("data: "):
                            # Split by lines, process each SSE event
                            for line in chunk_str.strip().split('\n'):
                                line = line.strip()
                                if not line:
                                    continue

                                if line == "data: [DONE]":
                                    print(f"  => Stream end marker")
                                elif line.startswith("data: "):
                                    try:
                                        json_str = line[6:]  # Strip "data: " prefix
                                        json_data = json.loads(json_str)
                                        print(f"  Parsed JSON: {json.dumps(json_data, indent=4, ensure_ascii=False)}")
                                    except Exception as e:
                                        print(f"  SSE parsing failed: {e}")
                    except Exception as e:
                        print(f"  Decoding failed: {e}")

            print(f"\nReceived a total of {chunk_count} chunks")

    # Run tests
    try:
        # Test non-streaming request
        test_non_stream_request()

        # Test streaming request
        test_stream_request()

        # Test streaming anti-truncation request
        test_anti_truncation_stream_request()

        print("\n" + "=" * 80)
        print("Tests completed")
        print("=" * 80)

    except Exception as e:
        print(f"\n❌ Exception occurred during test execution: {e}")
        traceback.print_exc()
