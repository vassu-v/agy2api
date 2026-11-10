"""
Anthropic to Gemini format converter

Provides comprehensive request body, response, and streaming conversion.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Response
from log import log
from src.converter.utils import merge_system_messages

from src.converter.thoughtSignature_fix import (
    decode_tool_id_and_signature,
    is_internal_placeholder_text,
    is_skip_thought_signature_placeholder,
    SKIP_THOUGHT_SIGNATURE_VALIDATOR,
)

DEFAULT_TEMPERATURE = 0.4
_DEBUG_TRUE = {"1", "true", "yes", "on"}

# ============================================================================
# Thinking Block Validation and Sanitization
# ============================================================================

# Minimum valid signature length
MIN_SIGNATURE_LENGTH = 10


def has_valid_thoughtsignature(block: Dict[str, Any]) -> bool:
    """
    Check if a thinking block has a valid signature.
    
    Args:
        block: Content block dictionary
        
    Returns:
        bool: Whether it has a valid signature
    """
    if not isinstance(block, dict):
        return True
    
    block_type = block.get("type")
    if block_type not in ("thinking", "redacted_thinking"):
        return True  # Non-thinking blocks are valid by default
    
    thinking = block.get("thinking", "")
    thoughtsignature = block.get("thoughtSignature")
    
    # Empty thinking + any thoughtsignature = valid (trailing signature case)
    if not thinking and thoughtsignature is not None:
        return True
    
    # Non-empty thinking + sufficient length thoughtsignature = valid
    if thoughtsignature and isinstance(thoughtsignature, str) and len(thoughtsignature) >= MIN_SIGNATURE_LENGTH:
        return True
    
    return False


def sanitize_thinking_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitize thinking block, retaining only essential fields (removes cache_control, etc.).
    
    Args:
        block: Content block dictionary
        
    Returns:
        Sanitized block dictionary
    """
    if not isinstance(block, dict):
        return block
    
    block_type = block.get("type")
    if block_type not in ("thinking", "redacted_thinking"):
        return block
    
    # Rebuild block, removing extra fields
    sanitized: Dict[str, Any] = {
        "type": block_type,
        "thinking": block.get("thinking", "")
    }
    
    thoughtsignature = block.get("thoughtSignature")
    if thoughtsignature:
        sanitized["thoughtSignature"] = thoughtsignature
    
    return sanitized


def remove_trailing_unsigned_thinking(blocks: List[Dict[str, Any]]) -> None:
    """
    Remove trailing unsigned thinking blocks.
    
    Args:
        blocks: Content blocks list (modified in place)
    """
    if not blocks:
        return
    
    # Scan backwards from end
    end_index = len(blocks)
    for i in range(len(blocks) - 1, -1, -1):
        block = blocks[i]
        if not isinstance(block, dict):
            break
        
        block_type = block.get("type")
        if block_type in ("thinking", "redacted_thinking"):
            if not has_valid_thoughtsignature(block):
                end_index = i
            else:
                break  # Stop when encountering a thinking block with valid signature
            break  # Stop when encountering a non-thinking block
    
    if end_index < len(blocks):
        removed = len(blocks) - end_index
        del blocks[end_index:]
        log.debug(f"Removed {removed} trailing unsigned thinking block(s)")


def filter_invalid_thinking_blocks(messages: List[Dict[str, Any]]) -> None:
    """
    Filter invalid thinking blocks from messages and sanitize extra fields (e.g., cache_control) from all thinking blocks.

    Args:
        messages: Anthropic messages list (modified in place)
    """
    total_filtered = 0

    for msg in messages:
        # Only process assistant and model messages
        role = msg.get("role", "")
        if role not in ("assistant", "model"):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        original_len = len(content)
        new_blocks: List[Dict[str, Any]] = []

        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue

            block_type = block.get("type")
            if block_type not in ("thinking", "redacted_thinking"):
                new_blocks.append(block)
                continue

            # All thinking blocks need sanitization (removing extra fields like cache_control)
            # Check thinking block validity
            if has_valid_thoughtsignature(block):
                # Valid signature, sanitize and keep
                new_blocks.append(sanitize_thinking_block(block))
            else:
                # Invalid signature, convert content to text block
                thinking_text = block.get("thinking", "")
                if thinking_text and str(thinking_text).strip():
                    log.info(
                        f"[Claude-Handler] Converting thinking block with invalid thoughtSignature to text. "
                        f"Content length: {len(thinking_text)} chars"
                    )
                    new_blocks.append({"type": "text", "text": thinking_text})
                else:
                    log.debug("[Claude-Handler] Dropping empty thinking block with invalid thoughtSignature")

        msg["content"] = new_blocks
        filtered_count = original_len - len(new_blocks)
        total_filtered += filtered_count

        # If empty after filtering, add an empty text block to keep the message valid
        if not new_blocks:
            msg["content"] = [{"type": "text", "text": ""}]

    if total_filtered > 0:
        log.debug(f"Filtered {total_filtered} invalid thinking block(s) from history")


# ============================================================================
# Request Validation and Extraction
# ============================================================================


def _anthropic_debug_enabled() -> bool:
    """Check if Anthropic debug mode is enabled."""
    return str(os.getenv("ANTHROPIC_DEBUG", "true")).strip().lower() in _DEBUG_TRUE


def _cached_content_token_count(usage_metadata: Any) -> int:
    if not isinstance(usage_metadata, dict):
        return 0
    return int(usage_metadata.get("cachedContentTokenCount", 0) or 0)


def _anthropic_usage_from_metadata(usage_metadata: Any) -> Dict[str, Any]:
    """
    Convert Gemini usageMetadata to Claude usage fields.

    Semantic alignment (Gemini -> Claude):
    - Gemini promptTokenCount includes cached tokens -> Claude input_tokens excludes cache
      (input_tokens = promptTokenCount - cachedContentTokenCount)
    - Gemini candidatesTokenCount excludes reasoning tokens -> Claude output_tokens includes reasoning tokens
      (output_tokens = candidatesTokenCount + thoughtsTokenCount)
    - Breakdown: cache_read_input_tokens / output_tokens_details.thinking_tokens
    """
    if not isinstance(usage_metadata, dict):
        return {"input_tokens": 0, "output_tokens": 0}

    prompt_tokens_total = int(usage_metadata.get("promptTokenCount", 0) or 0)
    cached_tokens = _cached_content_token_count(usage_metadata)
    reasoning_tokens = int(usage_metadata.get("thoughtsTokenCount", 0) or 0)
    usage: Dict[str, Any] = {
        "input_tokens": max(prompt_tokens_total - cached_tokens, 0),
        "output_tokens": (
            int(usage_metadata.get("candidatesTokenCount", 0) or 0)
            + reasoning_tokens
        ),
    }

    if cached_tokens > 0:
        usage["cache_read_input_tokens"] = cached_tokens

    if reasoning_tokens > 0:
        usage["output_tokens_details"] = {"thinking_tokens": reasoning_tokens}

    return usage


def _is_non_whitespace_text(value: Any) -> bool:
    """
    Determine whether text contains non-whitespace content.

    Note: Downstream (Antigravity/Claude compatibility layer) validates plain text content blocks:
    - text cannot be an empty string
    - text cannot consist solely of whitespace characters (spaces/newlines/tabs, etc.)
    """
    if value is None:
        return False
    try:
        return bool(str(value).strip())
    except Exception:
        return False


def _remove_nulls_for_tool_input(value: Any) -> Any:
    """
    Recursively remove fields/elements with null/None values from dict/list.

    Background: Under the Anthropic native tool path, if Roo/Kilo receives a tool_use.input containing null,
    it may treat null as a literal argument (e.g., "search in null").
    """
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in value.items():
            if v is None:
                continue
            cleaned[k] = _remove_nulls_for_tool_input(v)
        return cleaned

    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            if item is None:
                continue
            cleaned_list.append(_remove_nulls_for_tool_input(item))
        return cleaned_list

    return value

# ============================================================================
# 2. JSON Schema Sanitization
# ============================================================================

def clean_json_schema(schema: Any) -> Any:
    """
    Sanitize JSON Schema, removing fields unsupported downstream and appending validation constraints to description.
    """
    if not isinstance(schema, dict):
        return schema

    # Unsupported keys for downstream
    unsupported_keys = {
        "$schema", "$id", "$ref", "$defs", "definitions", "title",
        "example", "examples", "readOnly", "writeOnly", "default",
        "exclusiveMaximum", "exclusiveMinimum", "oneOf", "anyOf", "allOf",
        "const", "additionalItems", "contains", "patternProperties",
        "dependencies", "propertyNames", "if", "then", "else",
        "contentEncoding", "contentMediaType", "nullable",
    }

    validation_fields = {
        "minLength": "minLength",
        "maxLength": "maxLength",
        "minimum": "minimum",
        "maximum": "maximum",
        "minItems": "minItems",
        "maxItems": "maxItems",
    }
    fields_to_remove = {"additionalProperties"}

    validations: List[str] = []
    for field, label in validation_fields.items():
        if field in schema:
            validations.append(f"{label}: {schema[field]}")

    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in unsupported_keys or key in fields_to_remove or key in validation_fields:
            continue

        if key == "type" and isinstance(value, list):
            # type: ["string", "null"] -> type: "string", nullable: true
            has_null = any(
                isinstance(t, str) and t.strip() and t.strip().lower() == "null" for t in value
            )
            non_null_types = [
                t.strip()
                for t in value
                if isinstance(t, str) and t.strip() and t.strip().lower() != "null"
            ]

            cleaned[key] = non_null_types[0] if non_null_types else "string"
            continue

        if key == "description" and validations:
            cleaned[key] = f"{value} ({', '.join(validations)})"
        elif isinstance(value, dict):
            cleaned[key] = clean_json_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [clean_json_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value

    if validations and "description" not in cleaned:
        cleaned["description"] = f"Validation: {', '.join(validations)}"

    # If properties exist without an explicit type, supply "object"
    if "properties" in cleaned and "type" not in cleaned:
        cleaned["type"] = "object"

    if (
        isinstance(schema.get("properties"), dict)
        and isinstance(cleaned.get("required"), list)
    ):
        nullable_fields = {
            name
            for name, prop in schema["properties"].items()
            if isinstance(prop, dict)
            and isinstance(prop.get("type"), list)
            and any(str(t).lower() == "null" for t in prop["type"])
        }
        if nullable_fields:
            cleaned["required"] = [
                item for item in cleaned["required"]
                if item not in nullable_fields
            ]
            if not cleaned["required"]:
                cleaned.pop("required", None)

    return cleaned


# ============================================================================
# 4. Tools Conversion
# ============================================================================

def convert_tools(anthropic_tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """
    Convert Anthropic tools[] to downstream tools (functionDeclarations) structure.
    """
    if not anthropic_tools:
        return None

    gemini_tools: List[Dict[str, Any]] = []
    for tool in anthropic_tools:
        name = tool.get("name", "nameless_function")
        description = tool.get("description", "")
        input_schema = tool.get("input_schema", {}) or {}
        parameters = clean_json_schema(input_schema)

        gemini_tools.append(
            {
                "functionDeclarations": [
                    {
                        "name": name,
                        "description": description,
                        "parametersJsonSchema": parameters,
                    }
                ]
            }
        )

    return gemini_tools or None


# ============================================================================
# 5. Messages Conversion
# ============================================================================

def _extract_tool_result_output(content: Any) -> str:
    """Extract output string from tool_result.content."""
    if isinstance(content, list):
        if not content:
            return ""
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            return str(first.get("text", ""))
        return str(first)
    if content is None:
        return ""
    return str(content)


def convert_messages_to_contents(
    messages: List[Dict[str, Any]],
    *,
    include_thinking: bool = True
) -> List[Dict[str, Any]]:
    """
    Convert Anthropic messages[] to downstream contents[] (role: user/model, parts: []).

    Args:
        messages: List of messages in Anthropic format
        include_thinking: Whether to include thinking blocks
    """
    contents: List[Dict[str, Any]] = []

    # First pass: build mapping of tool_use_id -> (name, thoughtsignature)
    # Note: encoded ID is stored (may contain signature)
    tool_use_info: Dict[str, tuple[str, Optional[str]]] = {}
    for msg in messages:
        raw_content = msg.get("content", "")
        if isinstance(raw_content, list):
            for item in raw_content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    encoded_tool_id = item.get("id")
                    tool_name = item.get("name")
                    if encoded_tool_id and tool_name:
                        # Decode to obtain original ID and signature
                        original_id, thoughtsignature = decode_tool_id_and_signature(encoded_tool_id)
                        # Store mapping: encoded ID -> (name, thoughtsignature)
                        tool_use_info[str(encoded_tool_id)] = (tool_name, thoughtsignature)

    for msg in messages:
        role = msg.get("role", "user")
        
        # system/developer messages are already handled by merge_system_messages; skip here
        if role in ("system", "developer"):
            continue
        
        # Support 'assistant' and 'model' roles (Google history usage)
        gemini_role = "model" if role in ("assistant", "model") else "user"
        raw_content = msg.get("content", "")

        parts: List[Dict[str, Any]] = []
        if isinstance(raw_content, str):
            if _is_non_whitespace_text(raw_content):
                parts = [{"text": str(raw_content)}]
        elif isinstance(raw_content, list):
            for item in raw_content:
                if not isinstance(item, dict):
                    if _is_non_whitespace_text(item):
                        parts.append({"text": str(item)})
                    continue

                item_type = item.get("type")
                if item_type == "thinking":
                    # Do not echo client-returned thinking signatures back to Google.
                    # These signatures can easily become Corrupted thought signatures after relay/account switch/truncation.
                    continue
                elif item_type == "redacted_thinking":
                    continue
                elif item_type == "text":
                    text = item.get("text", "")
                    if _is_non_whitespace_text(text):
                        parts.append({"text": str(text)})
                elif item_type == "image":
                    source = item.get("source", {}) or {}
                    if source.get("type") == "base64":
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": source.get("media_type", "image/png"),
                                    "data": source.get("data", ""),
                                }
                            }
                        )
                elif item_type == "tool_use":
                    encoded_id = item.get("id") or ""
                    original_id, _ = decode_tool_id_and_signature(encoded_id)

                    fc_part: Dict[str, Any] = {
                        "functionCall": {
                            "id": original_id,  # Use original ID, without signature
                            "name": item.get("name"),
                            "args": item.get("input", {}) or {},
                        }
                    }

                    fc_part["thoughtSignature"] = SKIP_THOUGHT_SIGNATURE_VALIDATOR

                    parts.append(fc_part)
                elif item_type == "tool_result":
                    output = _extract_tool_result_output(item.get("content"))
                    encoded_tool_use_id = item.get("tool_use_id") or ""
                    
                    # Decode to get original ID (functionResponse does not need signature)
                    original_tool_use_id, _ = decode_tool_id_and_signature(encoded_tool_use_id)

                    # Get name from tool_result; look up in mapping if not present
                    func_name = item.get("name")
                    if not func_name and encoded_tool_use_id:
                        # Look up mapping using encoded ID
                        tool_info = tool_use_info.get(str(encoded_tool_use_id))
                        if tool_info:
                            func_name = tool_info[0]  # Get name
                    if not func_name:
                        func_name = "unknown_function"
                    
                    parts.append(
                        {
                            "functionResponse": {
                                "id": original_tool_use_id,  # Use decoded original ID to match functionCall
                                "name": func_name,
                                "response": {"output": output},
                            }
                        }
                    )
                else:
                    parts.append({"text": json.dumps(item, ensure_ascii=False)})
        else:
            if _is_non_whitespace_text(raw_content):
                parts = [{"text": str(raw_content)}]

        if not parts:
            continue

        contents.append({"role": gemini_role, "parts": parts})

    return contents


def reorganize_tool_messages(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Reorganize messages to satisfy tool_use/tool_result constraints.
    """
    tool_results: Dict[str, Dict[str, Any]] = {}

    for msg in contents:
        for part in msg.get("parts", []) or []:
            if isinstance(part, dict) and "functionResponse" in part:
                tool_id = (part.get("functionResponse") or {}).get("id")
                if tool_id:
                    tool_results[str(tool_id)] = part

    flattened: List[Dict[str, Any]] = []
    for msg in contents:
        role = msg.get("role")
        for part in msg.get("parts", []) or []:
            flattened.append({"role": role, "parts": [part]})

    new_contents: List[Dict[str, Any]] = []
    i = 0
    while i < len(flattened):
        msg = flattened[i]
        part = msg["parts"][0]

        if isinstance(part, dict) and "functionResponse" in part:
            i += 1
            continue

        if isinstance(part, dict) and "functionCall" in part:
            tool_id = (part.get("functionCall") or {}).get("id")
            new_contents.append({"role": "model", "parts": [part]})

            if tool_id is not None and str(tool_id) in tool_results:
                new_contents.append({"role": "user", "parts": [tool_results[str(tool_id)]]})

            i += 1
            continue

        new_contents.append(msg)
        i += 1

    return new_contents


# ============================================================================
# 7. Tool Choice Conversion
# ============================================================================

def convert_tool_choice_to_tool_config(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """
    Convert Anthropic tool_choice to Gemini toolConfig.

    Args:
        tool_choice: tool_choice in Anthropic format
            - {"type": "auto"}: Model automatically decides whether to use tools
            - {"type": "any"}: Model must use tools
            - {"type": "tool", "name": "tool_name"}: Model must use the specified tool

    Returns:
        Gemini format toolConfig, or None if invalid
    """
    if not tool_choice:
        return None
    
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        
        if choice_type == "auto":
            return {"functionCallingConfig": {"mode": "AUTO"}}
        elif choice_type == "any":
            return {"functionCallingConfig": {"mode": "ANY"}}
        elif choice_type == "tool":
            tool_name = tool_choice.get("name")
            if tool_name:
                return {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [tool_name],
                    }
                }
    
    # Invalid or unsupported tool_choice; return None
    return None


# ============================================================================
# 8. Generation Config Construction
# ============================================================================

def build_generation_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construct downstream generationConfig based on Anthropic Messages request.

    Returns:
        generation_config: Generation config dictionary
    """
    config: Dict[str, Any] = {
        "topP": 1,
        "candidateCount": 1,
        "stopSequences": [
            "<|user|>",
            "<|bot|>",
            "<|context_request|>",
            "<|endoftext|>",
            "<|end_of_turn|>",
        ],
    }

    temperature = payload.get("temperature", None)
    config["temperature"] = DEFAULT_TEMPERATURE if temperature is None else temperature

    top_p = payload.get("top_p", None)
    if top_p is not None:
        config["topP"] = top_p

    top_k = payload.get("top_k", None)
    if top_k is not None:
        config["topK"] = top_k

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        config["maxOutputTokens"] = max_tokens

    # Handle extended thinking parameters (plan mode)
    thinking = payload.get("thinking")
    is_plan_mode = False
    if thinking and isinstance(thinking, dict):
        thinking_type = thinking.get("type")
        budget_tokens = thinking.get("budget_tokens")
        
        # If extended thinking is enabled, set thinkingConfig
        if thinking_type == "enabled":
            is_plan_mode = True
            thinking_config: Dict[str, Any] = {}
            
            # Set thinking budget, defaulting to a larger value to support plan mode
            if budget_tokens is not None:
                thinking_config["thinkingBudget"] = budget_tokens
            else:
                # Default to a larger thinking budget to support complete plan generation
                thinking_config["thinkingBudget"] = 48000
            
            # Always include thoughts so the plan is visible
            thinking_config["includeThoughts"] = True
            
            config["thinkingConfig"] = thinking_config
            log.info(f"[ANTHROPIC2GEMINI] Extended thinking enabled with budget: {thinking_config['thinkingBudget']}")
        elif thinking_type == "disabled":
            # Explicitly disable thinking mode
            config["thinkingConfig"] = {
                "includeThoughts": False
            }
            log.info("[ANTHROPIC2GEMINI] Extended thinking explicitly disabled")

    stop_sequences = payload.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        config["stopSequences"] = config["stopSequences"] + [str(s) for s in stop_sequences]
    elif is_plan_mode:
        # In plan mode, clear default stop sequences to avoid premature stopping
        # Default stop sequences might cause the model to stop prematurely while generating plans
        config["stopSequences"] = []
        log.info("[ANTHROPIC2GEMINI] Plan mode: cleared default stop sequences to prevent premature stopping")
    
    # If not plan mode and no custom stop_sequences, keep default values
    # (Default values are already set during config initialization)

    return config


# ============================================================================
# 8. Main Conversion Functions
# ============================================================================

async def anthropic_to_gemini_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert Anthropic format request body to Gemini format request body.

    Note: This function only handles basic conversion and does not include logic in normalize_gemini_request
    (such as automatic thinking config setup, search tools, parameter range clamping, etc.)

    Args:
        payload: Request body dictionary in Anthropic format

    Returns:
        Request body dictionary in Gemini format, containing:
        - contents: Converted message contents
        - generationConfig: Generation config
        - systemInstruction: System instruction (if present)
        - tools: Tool definitions (if present)
        - toolConfig: Tool call configuration (if tool_choice is present)
    """
    # Handle consecutive system messages (compatibility mode)
    payload = await merge_system_messages(payload)

    # Extract and convert base information
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    
    # [CRITICAL FIX] Filter and fix Thinking block signatures
    # Filter invalid thinking blocks before conversion
    filter_invalid_thinking_blocks(messages)

    # Build generation config
    generation_config = build_generation_config(payload)

    # Convert message contents (always include thinking blocks; handled on the response side)
    contents = convert_messages_to_contents(messages, include_thinking=True)
    
    # [CRITICAL FIX] Remove trailing unsigned thinking blocks
    # Apply extra cleanup to real requests
    for content in contents:
        role = content.get("role", "")
        if role == "model":  # Only process model/assistant messages
            parts = content.get("parts", [])
            if isinstance(parts, list):
                remove_trailing_unsigned_thinking(parts)
    
    contents = reorganize_tool_messages(contents)

    # Convert tools
    tools = convert_tools(payload.get("tools"))
    
    # Convert tool_choice
    tool_config = convert_tool_choice_to_tool_config(payload.get("tool_choice"))

    # Build base request data
    gemini_request = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    
    # If merge_system_messages already added systemInstruction, use it
    if "systemInstruction" in payload:
        gemini_request["systemInstruction"] = payload["systemInstruction"]
    
    if tools:
        gemini_request["tools"] = tools

    # Add toolConfig (if tool_choice is present)
    if tool_config:
        gemini_request["toolConfig"] = tool_config

    # Pass through size parameter for image generation (e.g. "1024x1536")
    if "size" in payload and payload["size"]:
        gemini_request["size"] = payload["size"]

    return gemini_request


def gemini_to_anthropic_response(
    gemini_response: Dict[str, Any],
    model: str,
    status_code: int = 200
) -> Dict[str, Any]:
    """
    Convert Gemini format non-streaming response to Anthropic format non-streaming response.

    Note: If the received response body does not start with 200, forward directly without modification.

    Args:
        gemini_response: Response body dictionary in Gemini format
        model: Model name
        status_code: HTTP status code (default 200)

    Returns:
        Response body dictionary in Anthropic format, or original response (if status code is not 2xx)
    """
    # Non-2xx status codes return the original response directly
    if not (200 <= status_code < 300):
        return gemini_response

    # Handle Antigravity response wrapper format
    if "response" in gemini_response:
        response_data = gemini_response["response"]
    else:
        response_data = gemini_response

    # Extract candidate result
    candidate = response_data.get("candidates", [{}])[0] or {}
    parts = candidate.get("content", {}).get("parts", []) or []

    # Get usage metadata
    usage_metadata = {}
    if "usageMetadata" in response_data:
        usage_metadata = response_data["usageMetadata"]
    elif "usageMetadata" in candidate:
        usage_metadata = candidate["usageMetadata"]

    # Convert content blocks
    content = []
    has_tool_use = False

    for part in parts:
        if not isinstance(part, dict):
            continue

        # Handle thinking block
        if part.get("thought") is True:
            if is_skip_thought_signature_placeholder(part):
                continue
            thinking_text = part.get("text", "")
            if thinking_text is None:
                thinking_text = ""
            
            block: Dict[str, Any] = {"type": "thinking", "thinking": str(thinking_text)}
            
            # Add thoughtsignature if present
            thoughtsignature = part.get("thoughtSignature")
            if thoughtsignature:
                block["thoughtSignature"] = thoughtsignature
            
            content.append(block)
            continue

        # Handle text block
        if "text" in part:
            text = part.get("text", "")
            if (
                is_skip_thought_signature_placeholder(part)
                or is_internal_placeholder_text(text)
            ):
                continue
            content.append({"type": "text", "text": text})
            continue

        # Handle tool call
        if "functionCall" in part:
            has_tool_use = True
            fc = part.get("functionCall", {}) or {}
            original_id = fc.get("id") or f"toolu_{uuid.uuid4().hex}"
            content.append(
                {
                    "type": "tool_use",
                    "id": original_id,
                    "name": fc.get("name") or "",
                    "input": _remove_nulls_for_tool_input(fc.get("args", {}) or {}),
                }
            )
            continue

        # Handle image
        if "inlineData" in part:
            inline = part.get("inlineData", {}) or {}
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": inline.get("mimeType", "image/png"),
                        "data": inline.get("data", ""),
                    },
                }
            )
            continue

    # Determine stop reason
    finish_reason = candidate.get("finishReason")
    
    # Only set to tool_use on normal stop (STOP) with tool calls present
    # Avoid returning tool_use under SAFETY, MAX_TOKENS, etc., which causes loops
    if has_tool_use and finish_reason == "STOP":
        stop_reason = "tool_use"
    elif finish_reason == "MAX_TOKENS":
        stop_reason = "max_tokens"
    else:
        # Other cases (SAFETY, RECITATION, etc.) default to end_turn
        stop_reason = "end_turn"

    # Extract token usage
    usage = _anthropic_usage_from_metadata(usage_metadata)

    # Build Anthropic response
    message_id = f"msg_{uuid.uuid4().hex}"

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


async def gemini_stream_to_anthropic_stream(
    gemini_stream: AsyncIterator[bytes],
    model: str,
    status_code: int = 200
) -> AsyncIterator[bytes]:
    """
    Convert Gemini format streaming response to Anthropic SSE format streaming response.

    Note: If the received response body does not start with 200, forward directly without modification.

    Args:
        gemini_stream: Streaming response in Gemini format (bytes async iterator)
        model: Model name
        status_code: HTTP status code (default 200)

    Yields:
        Response chunk in Anthropic SSE format (bytes)
    """
    # Non-2xx status codes forward the original stream directly
    if not (200 <= status_code < 300):
        async for chunk in gemini_stream:
            yield chunk
        return

    # Initialize state
    message_id = f"msg_{uuid.uuid4().hex}"
    message_start_sent = False
    current_block_type: Optional[str] = None
    current_block_index = -1
    current_thinking_signature: Optional[str] = None
    has_tool_use = False
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    reasoning_tokens = 0
    finish_reason: Optional[str] = None
    stream_finished = False  # Consume but do not convert chunks after finishReason (keep upstream stream flowing to record request log)

    def _sse_event(event: str, data: Dict[str, Any]) -> bytes:
        """Generate SSE event."""
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")

    def _close_block() -> Optional[bytes]:
        """Close current content block."""
        nonlocal current_block_type
        if current_block_type is None:
            return None
        event = _sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": current_block_index},
        )
        current_block_type = None
        return event

    def _usage_payload() -> Dict[str, Any]:
        usage: Dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens + reasoning_tokens,
        }
        if cached_input_tokens > 0:
            usage["cache_read_input_tokens"] = cached_input_tokens
        if reasoning_tokens > 0:
            usage["output_tokens_details"] = {"thinking_tokens": reasoning_tokens}
        return usage

    # Process streaming data
    try:
        async for chunk in gemini_stream:
            # Check if it is a Response object (error condition)
            if isinstance(chunk, Response):
                log.warning(f"[GEMINI_TO_ANTHROPIC] Received Response object with status code {chunk.status_code}, forwarding error directly")
                # Forward error response content directly without format conversion
                error_content = chunk.body if isinstance(chunk.body, bytes) else chunk.body.encode('utf-8')
                yield error_content
                return

            # Consume without converting residual chunks after finishReason:
            # Breaking early sends GeneratorExit down the generator chain, preventing the underlying
            # stream_request success logging code from ever executing (issue #438)
            if stream_finished:
                continue

            # Parse Gemini streaming chunk
            if not chunk or not chunk.startswith(b"data: "):
                continue

            raw = chunk[6:].strip()
            if raw == b"[DONE]":
                # Same as finishReason: do not break, consume remaining chunks until stream ends naturally,
                # avoiding GeneratorExit to underlying stream_request which causes lost logs
                stream_finished = True
                continue

            try:
                data = json.loads(raw.decode('utf-8', errors='ignore'))
            except Exception as e:
                log.warning(f"[GEMINI_TO_ANTHROPIC] JSON parse error: {e}")
                continue

            # Handle Antigravity response wrapper format
            if "response" in data:
                response = data["response"]
            else:
                response = data

            candidate = (response.get("candidates", []) or [{}])[0] or {}
            parts = (candidate.get("content", {}) or {}).get("parts", []) or []

            # Update usage metadata
            if "usageMetadata" in response:
                usage = response["usageMetadata"]
                if isinstance(usage, dict):
                    if "promptTokenCount" in usage:
                        prompt_tokens_total = int(usage.get("promptTokenCount", 0) or 0)
                        input_tokens = max(prompt_tokens_total - cached_input_tokens, 0)
                    if "candidatesTokenCount" in usage:
                        output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
                    if "thoughtsTokenCount" in usage:
                        reasoning_tokens = int(usage.get("thoughtsTokenCount", 0) or 0)
                    if "cachedContentTokenCount" in usage:
                        cached_input_tokens = int(usage.get("cachedContentTokenCount", 0) or 0)
                        input_tokens = max(
                            int(usage.get("promptTokenCount", 0) or 0) - cached_input_tokens,
                            0,
                        )

            # Send message_start (only once)
            if not message_start_sent:
                message_start_sent = True
                yield _sse_event(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": message_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": _usage_payload(),
                        },
                    },
                )

            # Process various parts
            for part in parts:
                if not isinstance(part, dict):
                    continue

                # Handle thinking block
                if part.get("thought") is True:
                    if is_skip_thought_signature_placeholder(part):
                        continue
                    thinking_text = part.get("text", "")
                    thoughtsignature = part.get("thoughtSignature")
                    
                    # Check whether to close previous block and open new thinking block
                    if current_block_type != "thinking":
                        close_evt = _close_block()
                        if close_evt:
                            yield close_evt

                        current_block_index += 1
                        current_block_type = "thinking"
                        current_thinking_signature = thoughtsignature

                        block: Dict[str, Any] = {"type": "thinking", "thinking": ""}
                        if thoughtsignature:
                            block["thoughtSignature"] = thoughtsignature
                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": block,
                            },
                        )
                    elif thoughtsignature and thoughtsignature != current_thinking_signature:
                        # Signature changed, need to open new thinking block
                        close_evt = _close_block()
                        if close_evt:
                            yield close_evt
                        
                        current_block_index += 1
                        current_block_type = "thinking"
                        current_thinking_signature = thoughtsignature
                        
                        block_new: Dict[str, Any] = {"type": "thinking", "thinking": ""}
                        if thoughtsignature:
                            block_new["thoughtSignature"] = thoughtsignature
                        
                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": block_new,
                            },
                        )

                    # Send thinking text delta
                    if thinking_text:
                        yield _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": current_block_index,
                                "delta": {"type": "thinking_delta", "thinking": thinking_text},
                            },
                        )
                    continue

                # Handle text block
                if "text" in part:
                    if (
                        is_skip_thought_signature_placeholder(part)
                        or is_internal_placeholder_text(part.get("text"))
                    ):
                        continue
                    text = part.get("text", "")
                    if isinstance(text, str) and not text.strip():
                        continue

                    if current_block_type != "text":
                        close_evt = _close_block()
                        if close_evt:
                            yield close_evt

                        current_block_index += 1
                        current_block_type = "text"

                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )

                    if text:
                        yield _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": current_block_index,
                                "delta": {"type": "text_delta", "text": text},
                            },
                        )
                    continue

                # Handle tool call
                if "functionCall" in part:
                    close_evt = _close_block()
                    if close_evt:
                        yield close_evt

                    has_tool_use = True
                    fc = part.get("functionCall", {}) or {}
                    original_id = fc.get("id") or f"toolu_{uuid.uuid4().hex}"
                    tool_id = original_id
                    tool_name = fc.get("name") or ""
                    tool_args = _remove_nulls_for_tool_input(fc.get("args", {}) or {})

                    if _anthropic_debug_enabled():
                        log.info(
                            f"[ANTHROPIC][tool_use] Process tool call: name={tool_name}, "
                            f"id={tool_id}"
                        )

                    current_block_index += 1
                    # Note: tool_use does not set current_block_type as it is a self-contained block

                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": current_block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": {},
                            },
                        },
                    )

                    input_json = json.dumps(tool_args, ensure_ascii=False, separators=(",", ":"))
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": current_block_index,
                            "delta": {"type": "input_json_delta", "partial_json": input_json},
                        },
                    )

                    yield _sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": current_block_index},
                    )
                    # Tool call block is fully closed, current_block_type remains None
                    
                    if _anthropic_debug_enabled():
                        log.info(f"[ANTHROPIC][tool_use] Tool call block closed: index={current_block_index}")
                    
                    continue

            # Check if finished
            if candidate.get("finishReason"):
                finish_reason = candidate.get("finishReason")
                # Do not break: continue consuming upstream residual chunks (e.g. [DONE]),
                # ensuring underlying stream_request takes normal teardown path to record request log (issue #438)
                stream_finished = True

        # Close last content block
        close_evt = _close_block()
        if close_evt:
            yield close_evt

        # Determine stop reason
        # Only set to tool_use on normal stop (STOP) with tool calls present
        # Avoid returning tool_use under SAFETY, MAX_TOKENS, etc., which causes loops
        if has_tool_use and finish_reason == "STOP":
            stop_reason = "tool_use"
        elif finish_reason == "MAX_TOKENS":
            stop_reason = "max_tokens"
        else:
            # Other cases (SAFETY, RECITATION, etc.) default to end_turn
            stop_reason = "end_turn"

        if _anthropic_debug_enabled():
            log.info(
                f"[ANTHROPIC][stream_end] Stream ended: stop_reason={stop_reason}, "
                f"has_tool_use={has_tool_use}, finish_reason={finish_reason}, "
                f"input_tokens={input_tokens}, output_tokens={output_tokens}"
            )

        # Send message_delta and message_stop
        yield _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": _usage_payload(),
            },
        )

        yield _sse_event("message_stop", {"type": "message_stop"})

    except Exception as e:
        log.error(f"[ANTHROPIC] Streaming conversion failed: {e}")
        # Send error event
        if not message_start_sent:
            yield _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": _usage_payload(),
                    },
                },
            )
        yield _sse_event(
            "error",
            {"type": "error", "error": {"type": "api_error", "message": str(e)}},
        )
