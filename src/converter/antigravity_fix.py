"""
Antigravity Format Utilities - Independent Antigravity request processing and conversion utilities.

Design notes:
  * Each model family has an independent line of configuration in resolve_profile (backend model ID / thinking mode).
  * All models disable prefill (profile.no_prefill is always True).
  * Does not mutate caller-supplied request (generationConfig / thinkingConfig / contents are copied first).
────────────────────────────────────────────────────────────────
Project requirements:
- Use print for warning/error output; debug logs removed.
"""
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import (
    apply_sensitive_word_filter,
    get_model_rewrite_filter,
    get_return_thoughts_to_frontend,
    get_sensitive_word_filter,
    rewrite_model_name,
)
from src.converter.thoughtSignature_fix import SKIP_THOUGHT_SIGNATURE_VALIDATOR

# ==================== Constant Configurations ====================

# Base model name for image generation (only modify here when upstream changes)
IMAGE_BASE_MODEL = "gemini-3.1-flash-image"

# Supported aspect ratios for image generation
SUPPORTED_ASPECT_RATIOS = [
    (1, 1), (2, 3), (3, 2), (3, 4), (4, 3),
    (4, 5), (5, 4), (9, 16), (16, 9), (21, 9),
]

# ==================== Gemini API Safety Settings ====================

DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_HATE", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_JAILBREAK", "threshold": "OFF"},
]

MAX_OUTPUT_TOKENS = 64000
TOP_K = 64
_CLAUDE_THINKING_SIGNATURE = "skip_thought_signature_validator"  # Virtual signature recommended by official documentation


def _append_schema_hint(schema: Dict[str, Any], hint: str) -> None:
    """Move fragile validation details into description instead of sending them raw."""
    if not hint:
        return
    desc = schema.get("description")
    schema["description"] = f"{desc} ({hint})" if desc else hint


def _resolve_schema_ref(ref: str, root_schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None

    node: Any = root_schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]

    return node if isinstance(node, dict) else None


def _clean_parameters_json_schema(
    schema: Any,
    root_schema: Optional[Dict[str, Any]] = None,
    visited: Optional[set] = None,
) -> Any:
    """Clean a tool schema for Code Assist's parametersJsonSchema field."""
    if isinstance(schema, list):
        return [_clean_parameters_json_schema(item, root_schema, visited) for item in schema]
    if not isinstance(schema, dict):
        return schema

    if root_schema is None:
        root_schema = schema
    if visited is None:
        visited = set()

    schema_id = id(schema)
    if schema_id in visited:
        return {"type": "object", "description": "circular reference"}
    visited.add(schema_id)

    ref_key = "$ref" if "$ref" in schema else ("ref" if "ref" in schema else None)
    if ref_key:
        resolved = _resolve_schema_ref(schema[ref_key], root_schema)
        if resolved:
            merged = dict(resolved)
            for key in ("description", "default"):
                if key in schema:
                    merged[key] = schema[key]
            schema = merged

    if "allOf" in schema:
        result: Dict[str, Any] = {}
        for item in schema.get("allOf") or []:
            cleaned_item = _clean_parameters_json_schema(item, root_schema, visited)
            if not isinstance(cleaned_item, dict):
                continue
            if "properties" in cleaned_item:
                result.setdefault("properties", {}).update(cleaned_item["properties"])
            if "required" in cleaned_item:
                result.setdefault("required", []).extend(cleaned_item["required"])
            for key, value in cleaned_item.items():
                if key not in ("properties", "required"):
                    result[key] = value
        for key, value in schema.items():
            if key not in ("allOf", "properties", "required"):
                result[key] = value
            elif key in ("properties", "required") and key not in result:
                result[key] = value
    else:
        result = dict(schema)

    if result.get("nullable") is True:
        _append_schema_hint(result, "nullable")

    if "type" in result:
        type_value = result["type"]
        if isinstance(type_value, list):
            non_null_types = [
                str(t).lower()
                for t in type_value
                if isinstance(t, str) and t.lower() != "null"
            ]
            if non_null_types:
                result["type"] = non_null_types[0]
                if any(str(t).lower() == "null" for t in type_value):
                    _append_schema_hint(result, "nullable")
            else:
                result["type"] = "string"
        elif isinstance(type_value, str):
            lower_type = type_value.lower()
            if lower_type in {"string", "number", "integer", "boolean", "array", "object"}:
                result["type"] = lower_type
            elif lower_type == "null":
                result["type"] = "string"
                _append_schema_hint(result, "nullable")
            else:
                result.pop("type", None)

    if "anyOf" in result or "oneOf" in result:
        union_key = "anyOf" if "anyOf" in result else "oneOf"
        union_items = result.get(union_key) or []
        cleaned_items = [
            item for item in (
                _clean_parameters_json_schema(item, root_schema, visited)
                for item in union_items
            )
            if isinstance(item, dict)
        ]
        enum_values = [
            item.get("const")
            for item in union_items
            if isinstance(item, dict) and item.get("const") not in ("", None)
        ]
        if enum_values and len(enum_values) == len(union_items):
            result["type"] = "string"
            result["enum"] = [str(v) for v in enum_values]
        else:
            preferred = next(
                (
                    item for item in cleaned_items
                    if item.get("type") in ("object", "array") or item.get("properties")
                ),
                None,
            )
            if preferred is None:
                preferred = next((item for item in cleaned_items if item.get("type") or item.get("enum")), None)
            if preferred:
                original_description = result.get("description")
                result.update(preferred)
                if original_description:
                    _append_schema_hint(result, original_description)
        result.pop("anyOf", None)
        result.pop("oneOf", None)

    if result.get("type") == "array":
        items = result.get("items")
        if isinstance(items, list):
            if items:
                result["items"] = _clean_parameters_json_schema(items[0], root_schema, visited)
                _append_schema_hint(result, "tuple schema simplified")
            else:
                result.pop("items", None)
        elif isinstance(items, dict):
            result["items"] = _clean_parameters_json_schema(items, root_schema, visited)

    validation_keys = {
        "default", "minLength", "maxLength", "minimum", "maximum",
        "minItems", "maxItems", "pattern", "format", "uniqueItems",
    }
    for key in list(result.keys()):
        if key in validation_keys:
            value = result.pop(key)
            if value not in (None, "", {}, []):
                _append_schema_hint(result, f"{key}: {json.dumps(value, ensure_ascii=False)}")

    unsupported_keys = {
        "title", "$schema", "$id", "$ref", "ref", "strict", "nullable",
        "exclusiveMaximum", "exclusiveMinimum", "additionalProperties",
        "allOf", "anyOf", "oneOf", "$defs", "definitions", "example",
        "examples", "readOnly", "writeOnly", "const", "additionalItems",
        "contains", "patternProperties", "dependencies", "propertyNames",
        "if", "then", "else", "contentEncoding", "contentMediaType",
    }
    for key in list(result.keys()):
        if key in unsupported_keys or key.startswith("x-"):
            del result[key]

    nullable_props = set()
    if isinstance(result.get("properties"), dict):
        cleaned_props = {}
        for prop_name, prop_schema in result["properties"].items():
            if isinstance(prop_schema, dict):
                prop_type = prop_schema.get("type")
                if (
                    prop_schema.get("nullable") is True
                    or (
                        isinstance(prop_type, list)
                        and any(str(t).lower() == "null" for t in prop_type)
                    )
                ):
                    nullable_props.add(prop_name)
            cleaned_props[prop_name] = _clean_parameters_json_schema(prop_schema, root_schema, visited)
        result["properties"] = cleaned_props

    if "properties" in result and "type" not in result:
        result["type"] = "object"

    if isinstance(result.get("required"), list):
        prop_names = set(result.get("properties", {}).keys()) if isinstance(result.get("properties"), dict) else None
        required = []
        for item in result["required"]:
            if not isinstance(item, str):
                continue
            if prop_names is not None and item not in prop_names:
                continue
            if item in nullable_props:
                continue
            if item not in required:
                required.append(item)
        if required:
            result["required"] = required
        else:
            result.pop("required", None)

    return result


def _normalize_tools_for_internal_api(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools

    normalized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized_tools.append(tool)
            continue

        normalized_tool = tool.copy()
        declarations = normalized_tool.get("functionDeclarations")
        if declarations is None:
            declarations = normalized_tool.get("function_declarations")
        if isinstance(declarations, list):
            normalized_declarations = []
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    normalized_declarations.append(declaration)
                    continue

                normalized_declaration = declaration.copy()
                if "parametersJsonSchema" in normalized_declaration:
                    schema = normalized_declaration["parametersJsonSchema"]
                elif "parameters_json_schema" in normalized_declaration:
                    schema = normalized_declaration.pop("parameters_json_schema", None)
                else:
                    schema = normalized_declaration.pop("parameters", None)

                normalized_declaration.pop("parameters", None)
                normalized_declaration.pop("parameters_json_schema", None)
                if schema not in (None, {}, []):
                    normalized_declaration["parametersJsonSchema"] = _clean_parameters_json_schema(schema)
                else:
                    normalized_declaration.pop("parametersJsonSchema", None)

                normalized_declarations.append(normalized_declaration)

            normalized_tool.pop("function_declarations", None)
            normalized_tool["functionDeclarations"] = normalized_declarations

        normalized_tools.append(normalized_tool)

    return normalized_tools


def _ensure_empty_tool_schema_for_claude(tools: Any, model_name: str) -> Any:
    if not isinstance(tools, list):
        return tools

    is_claude = "claude" in (model_name or "").lower()

    if is_claude:
        normalized_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                normalized_tools.append(tool)
                continue

            normalized_tool = tool.copy()

            schema = {"type": "object", "properties": {}}
            name = ""
            description = ""

            # Extract schema from either format
            custom_tool = normalized_tool.get("custom")
            if isinstance(custom_tool, dict):
                schema = custom_tool.get("input_schema") or custom_tool.get("inputSchema") or schema
                name = custom_tool.get("name", "")
                description = custom_tool.get("description", "")
            else:
                declarations = normalized_tool.get("functionDeclarations") or normalized_tool.get("function_declarations")
                if isinstance(declarations, list) and declarations and isinstance(declarations[0], dict):
                    decl = declarations[0]
                    schema = (
                        decl.get("parametersJsonSchema") or
                        decl.get("parameters_json_schema") or
                        decl.get("parameters") or schema
                    )
                    name = decl.get("name", "")
                    description = decl.get("description", "")

            # For ALL Claude models, try outputting functionDeclarations with parameters!
            # If Google's backend expects parameters to translate to input_schema, this will fix the Field required error.
            normalized_tools.append({
                "functionDeclarations": [{
                    "name": name,
                    "description": description,
                    "parameters": schema
                }]
            })

        return normalized_tools

    # For Gemini models:
    # Backend requires standard functionDeclarations format.
    # Furthermore, only the "parameters" field must be used; if "parametersJsonSchema" is used,
    # it causes conflicts like "parameters_json_schema must not be set when parameters is set".
    normalized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized_tools.append(tool)
            continue

        normalized_tool = tool.copy()

        # 1. If Anthropic native "custom" tool format is present, convert it to Gemini functionDeclarations format
        custom_tool = normalized_tool.get("custom")
        if isinstance(custom_tool, dict):
            schema = custom_tool.get("input_schema") or custom_tool.get("inputSchema")
            if schema in (None, {}, []):
                schema = {"type": "object", "properties": {}}
            declaration = {
                "name": custom_tool.get("name", ""),
                "description": custom_tool.get("description", ""),
                "parameters": schema
            }
            normalized_tools.append({
                "functionDeclarations": [declaration]
            })
            continue

        # 2. If standard functionDeclarations format is present, ensure parameters is non-empty and only use parameters field
        declarations = normalized_tool.get("functionDeclarations") or normalized_tool.get("function_declarations")
        if isinstance(declarations, list):
            normalized_declarations = []
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    normalized_declarations.append(declaration)
                    continue

                normalized_declaration = declaration.copy()
                # Normalize various field formats to parameters
                schema = (
                    normalized_declaration.get("parameters")
                    or normalized_declaration.get("parametersJsonSchema")
                    or normalized_declaration.get("parameters_json_schema")
                )

                if schema in (None, {}, []):
                    schema = {"type": "object", "properties": {}}

                # Retain only the parameters field to prevent conflict with parametersJsonSchema
                normalized_declaration["parameters"] = schema
                normalized_declaration.pop("parametersJsonSchema", None)
                normalized_declaration.pop("parameters_json_schema", None)

                normalized_declarations.append(normalized_declaration)

            normalized_tool.pop("function_declarations", None)
            normalized_tool["functionDeclarations"] = normalized_declarations

        normalized_tools.append(normalized_tool)

    return normalized_tools


def _should_skip_thought_signature(part: Dict[str, Any], model_name: str) -> bool:
    if "claude" in (model_name or "").lower():
        return False

    return (
        "functionCall" in part
        or "function_call" in part
        or part.get("thought") is True
        or "thoughtSignature" in part
        or "thought_signature" in part
    )


def _normalize_part_thought_signature(part: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    normalized = part.copy()
    if _should_skip_thought_signature(normalized, model_name):
        normalized.pop("thought_signature", None)
        normalized["thoughtSignature"] = SKIP_THOUGHT_SIGNATURE_VALIDATOR
    return normalized


def _ensure_tool_call_ids(contents: Any, model_name: str) -> Any:
    """
    Ensure functionCall/functionResponse carry an id field (Claude only).

    When the target model is Claude, the Antigravity backend internally converts
    Gemini functionCall/functionResponse to Anthropic tool_use/tool_result,
    where id is a required field. Native Gemini requests may lack an id,
    so missing ids are filled in based on name to ensure functionCall and
    functionResponse for the same call share the same id. Does not mutate inputs.
    """
    if "claude" not in (model_name or "").lower():
        return contents
    if not isinstance(contents, list):
        return contents

    pending_ids_by_name: Dict[str, list] = {}
    result = []

    for content in contents:
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            result.append(content)
            continue

        new_parts = []
        for part in content["parts"]:
            if isinstance(part, dict):
                fc = part.get("functionCall")
                fr = part.get("functionResponse")
                if isinstance(fc, dict):
                    if not fc.get("id"):
                        fc = {**fc, "id": f"toolu_{uuid.uuid4().hex}"}
                        part = {**part, "functionCall": fc}
                    pending_ids_by_name.setdefault(fc.get("name"), []).append(fc["id"])
                elif isinstance(fr, dict):
                    queue = pending_ids_by_name.get(fr.get("name")) or []
                    if fr.get("id"):
                        if fr["id"] in queue:
                            queue.remove(fr["id"])
                    else:
                        new_id = queue.pop(0) if queue else f"toolu_{uuid.uuid4().hex}"
                        part = {**part, "functionResponse": {**fr, "id": new_id}}
            new_parts.append(part)

        result.append({**content, "parts": new_parts})

    return result


def _parse_size_to_image_config(size_str: str) -> Dict[str, str]:
    """
    Parse user-supplied size parameter into Gemini imageConfig parameters.

    Supported formats: "1024x1536", "1024*1536", "1024X1536"

    Returns:
        Dictionary containing aspectRatio and/or imageSize
    """
    config = {}
    size_str = size_str.strip()

    match = re.match(r"^(\d+)\s*[xX*×]\s*(\d+)$", size_str)
    if not match:
        return config

    width, height = int(match.group(1)), int(match.group(2))

    if width <= 0 or height <= 0:
        return config

    # Calculate closest supported aspect ratio
    target_ratio = width / height
    best_ratio = None
    best_diff = float("inf")
    for w, h in SUPPORTED_ASPECT_RATIOS:
        diff = abs(target_ratio - w / h)
        if diff < best_diff:
            best_diff = diff
            best_ratio = f"{w}:{h}"
    if best_ratio:
        config["aspectRatio"] = best_ratio

    # Determine imageSize based on the maximum dimension (using closest tier)
    max_dim = max(width, height)
    if max_dim <= 1280:
        config["imageSize"] = "1K"
    elif max_dim <= 2560:
        config["imageSize"] = "2K"
    else:
        config["imageSize"] = "4K"

    return config


def prepare_image_generation_request(
    request_body: Dict[str, Any],
    model: str
) -> Dict[str, Any]:
    """
    Post-process request body for image generation models.

    Supports three methods to specify image parameters (in descending order of priority):
    1. size parameter: e.g. "1024x1536", automatically calculates aspectRatio and imageSize
    2. Model name suffix: e.g. -4k, -2k, -16x9, -1x1
    3. Default value: do not set extra parameters
    """
    request_body = request_body.copy()
    model_lower = model.lower()

    # Prioritize size parameter
    size_str = request_body.pop("size", None)
    if size_str:
        image_config = _parse_size_to_image_config(size_str)
    else:
        # Parse from model name suffix
        image_size = "4K" if "-4k" in model_lower else "2K" if "-2k" in model_lower else None

        aspect_ratio = None
        for suffix, ratio in [
            ("-21x9", "21:9"), ("-16x9", "16:9"), ("-9x16", "9:16"),
            ("-4x3", "4:3"), ("-3x4", "3:4"), ("-1x1", "1:1")
        ]:
            if suffix in model_lower:
                aspect_ratio = ratio
                break

        image_config = {}
        if aspect_ratio:
            image_config["aspectRatio"] = aspect_ratio
        if image_size:
            image_config["imageSize"] = image_size

    request_body["model"] = IMAGE_BASE_MODEL  # Standardize to base model name
    request_body["generationConfig"] = {
        "candidateCount": 1,
        "imageConfig": image_config
    }

    # Remove unneeded fields
    for key in ("systemInstruction", "tools", "toolConfig"):
        request_body.pop(key, None)

    return request_body


# ==================== Model Configuration (Keyword Matching) ====================

@dataclass(frozen=True)
class ModelProfile:
    family: str                     # "gemini4" | "gemini3" | "claude" | "image" | "other"
    upstream: str                   # Actual model ID sent to Antigravity backend
    thinking: str = "none"          # "route" | "budget" | "none", see _apply_thinking
    thinking_budget: int = 1024     # Only used when thinking == "budget"
    no_prefill: bool = True         # All models disable prefill


# Exact aliases: client model name -> backend real ID
_UPSTREAM_ALIASES: Dict[str, str] = {
    # gemini-3.1-pro-* has been retired from upstream model list, alias removed
}


async def _get_model_rewrite_rules():
    """Read effective model name rewrite rules; returns None when disabled."""
    enabled, rules = await get_model_rewrite_filter()
    return rules if enabled and rules else None


def resolve_profile(model: str) -> ModelProfile:
    """
    Match model by keyword, the first matching rule from top to bottom takes effect:

      image: image generation, takes independent path
      claude-opus-5-5 / claude-sonnet-5-5: thinking strength determined by model ID suffix, pass through as-is
      claude-opus-4-6: map to claude-opus-4-6-thinking
      claude-sonnet-4-6-thinking: map to claude-sonnet-4-6
      claude (others): pass through as-is
      gemini-4*: (e.g. gemini-4-argon-low/-medium/-high) thinking depth determined by model ID suffix, pass through as-is
      gemini-3* / *-agent: thinking depth determined by model ID (-low/-medium/-high/-tiered/-extra-low)
      others: only send thinkingBudget if name contains think, pass through as-is
    """
    model = model or ""
    lower = model.lower()
    think = "budget" if "think" in lower else "none"

    if "image" in lower:
        return ModelProfile("image", model)

    if "claude" in lower:
        # Opus 5.5 / Sonnet 5.5: thinking strength determined by model ID suffix (-low/-high etc.), pass through as-is
        normalized = lower.replace(".", "-")
        if "claude-opus-5-5" in normalized or "claude-sonnet-5-5" in normalized:
            return ModelProfile("claude", model, "route")

        upstream = model
        if "claude-opus-4-6" in lower:
            upstream = "claude-opus-4-6-thinking"
        elif "claude-sonnet-4-6-thinking" in lower:
            upstream = "claude-sonnet-4-6"
        return ModelProfile("claude", upstream, think)

    if "gemini-4" in lower:
        return ModelProfile("gemini4", _UPSTREAM_ALIASES.get(lower, model), "route")

    if "gemini-3" in lower or lower.endswith("-agent"):
        return ModelProfile("gemini3", _UPSTREAM_ALIASES.get(lower, model), "route")

    return ModelProfile("other", model, think)


# ==================== Model Feature Helper Functions ====================

def is_thinking_model(model_name: str) -> bool:
    """Check if model is a thinking model (model name contains think, kept for compatibility)."""
    return "think" in model_name.lower()


def _apply_thinking(profile: ModelProfile, generation_config: Dict[str, Any], return_thoughts: bool) -> None:
    """Write thinkingConfig based on model profile (modifies generation_config in-place, thinkingConfig copied first)."""
    if profile.thinking == "none":
        return

    thinking_config = dict(generation_config.get("thinkingConfig") or {})
    thinking_config.pop("thinkingLevel", None)
    if profile.thinking == "route":
        # Thinking depth is determined by model ID; level/budget would conflict with it
        thinking_config.pop("thinkingBudget", None)
    else:  # "budget"
        thinking_config["thinkingBudget"] = profile.thinking_budget
    thinking_config["includeThoughts"] = return_thoughts
    generation_config["thinkingConfig"] = thinking_config


# ==================== contents Processing ====================

async def _get_sensitive_word_patterns():
    """Read effective sensitive word regex list; returns None when disabled (negligible overhead when 0)."""
    enabled, patterns = await get_sensitive_word_filter()
    return patterns if enabled and patterns else None


def _filter_text(text: str, patterns) -> str:
    """Apply sensitive word filtering to text (case-insensitive, matched words replaced with empty string)."""
    return apply_sensitive_word_filter(text, patterns)


def _filter_contents_sensitive_words(contents: Any, patterns) -> Any:
    """Apply sensitive word filtering to all text parts in contents (does not mutate inputs)."""
    if not isinstance(contents, list):
        return contents

    new_contents = []
    for content in contents:
        if not (isinstance(content, dict) and isinstance(content.get("parts"), list)):
            new_contents.append(content)
            continue

        new_parts = []
        for part in content["parts"]:
            if (
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and part["text"]
            ):
                filtered = _filter_text(part["text"], patterns)
                if filtered != part["text"]:
                    part = {**part, "text": filtered}
            new_parts.append(part)

        new_contents.append({**content, "parts": new_parts})
    return new_contents


def _filter_system_instruction_sensitive_words(system_instruction: Any, patterns) -> Any:
    """Apply sensitive word filtering to text parts in systemInstruction (does not mutate inputs)."""
    if not (isinstance(system_instruction, dict) and isinstance(system_instruction.get("parts"), list)):
        return system_instruction

    new_parts = []
    for part in system_instruction["parts"]:
        if (
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and part["text"]
        ):
            filtered = _filter_text(part["text"], patterns)
            if filtered != part["text"]:
                part = {**part, "text": filtered}
        new_parts.append(part)

    return {**system_instruction, "parts": new_parts}


def _clean_contents(contents: List[Any], model_name: str) -> List[Any]:
    """Filter empty parts, normalize thoughtSignature, fix text field type, discard contents without valid parts."""
    cleaned_contents = []
    for content in contents:
        if not (isinstance(content, dict) and "parts" in content):
            cleaned_contents.append(content)
            continue

        valid_parts = []
        for part in content["parts"]:
            if not isinstance(part, dict):
                continue

            # Must normalize text first (rstrip may reduce "\n" etc. to "") before validity check;
            # otherwise whitespace-only text passes validation and becomes empty, turning into a text block
            # lacking a text field when sent upstream (Claude reports messages.N.content.M.text.text: Field required).
            if "text" in part:
                part = part.copy()  # copy-on-write, avoid modifying caller's contents in-place
                text_value = part["text"]
                if isinstance(text_value, list):
                    # Element may be {"type":"text","text":"..."}, cannot str(dict) directly or it will pollute model history
                    print(f"[ANTIGRAVITY_FIX] text field is a list, automatically merging: {text_value}", flush=True)
                    text_parts = []
                    for t in text_value:
                        if isinstance(t, dict) and "text" in t:
                            text_parts.append(str(t["text"]))
                        elif isinstance(t, str):
                            text_parts.append(t)
                        elif t is not None:
                            text_parts.append(str(t))
                    part["text"] = " ".join(text_parts)
                elif isinstance(text_value, str):
                    part["text"] = text_value.rstrip()
                else:
                    print(f"[ANTIGRAVITY_FIX] abnormal text field type ({type(text_value)}), converting to string: {text_value}", flush=True)
                    part["text"] = str(text_value)

                if part["text"] == "":
                    # When part has other valid fields (e.g. functionCall), only remove text field
                    part.pop("text", None)

            # thought field can be empty; remaining fields must have at least one non-empty value
            has_valid_value = any(
                value not in (None, "", {}, [])
                for key, value in part.items()
                if key != "thought"
            )
            if not has_valid_value:
                print(f"[ANTIGRAVITY_FIX] Removing empty or invalid part: {part}", flush=True)
                continue

            part = _normalize_part_thought_signature(part, model_name)

            valid_parts.append(part)

        if valid_parts:
            cleaned_content = content.copy()
            cleaned_content["parts"] = valid_parts
            cleaned_contents.append(cleaned_content)
        else:
            print(f"[ANTIGRAVITY_FIX] Skipping content without valid parts: {content.get('role')}", flush=True)
    return cleaned_contents


def _strip_trailing_model_turns(contents: List[Any]) -> Tuple[List[Any], int]:
    """Iteratively remove trailing model messages to ensure ending with a user message."""
    contents = list(contents)
    removed = 0
    while contents and isinstance(contents[-1], dict) and contents[-1].get("role") == "model":
        contents.pop()
        removed += 1
    return contents, removed


def _prepare_claude_history(contents: List[Any], generation_config: Dict[str, Any]) -> List[Any]:
    """
    Claude-specific history processing:
    - Contains tool calls (MCP scenario): remove thinkingConfig to avoid failure
    - Otherwise: prepend a thinking block to the last model message (dummy signature skips validation)
    """
    has_tool_calls = any(
        isinstance(content, dict)
        and any(
            isinstance(part, dict) and ("functionCall" in part or "function_call" in part)
            for part in content.get("parts", []) or []
        )
        for content in contents
    )
    if has_tool_calls:
        print("[ANTIGRAVITY] Detected tool calls (MCP scenario), removing thinkingConfig to avoid failure", flush=True)
        generation_config.pop("thinkingConfig", None)
        return contents

    contents = list(contents)
    for i in range(len(contents) - 1, -1, -1):
        content = contents[i]
        if isinstance(content, dict) and content.get("role") == "model":
            parts = content.get("parts", []) or []
            first = parts[0] if parts else None
            if not (isinstance(first, dict) and ("thought" in first or "thoughtSignature" in first)):
                thinking_part = {"text": "...", "thoughtSignature": _CLAUDE_THINKING_SIGNATURE}
                contents[i] = {**content, "parts": [thinking_part] + list(parts)}
            break
    return contents


# ==================== Unified Antigravity Request Post-Processing ====================

async def normalize_antigravity_request(
    request: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Normalize Antigravity request.

    Processing logic:
    0. Sensitive word filtering (contents / systemInstruction, panel-configurable, hot-reload)
    0.5 Model name rewrite (panel-configurable, hot-reload, supports regex and backreferences)
    1. Resolve profile by model name (keyword matching)
    2. Image models take independent path
    3. Model mapping + thinkingConfig (by profile.thinking)
    4. contents cleanup -> prefill handling -> Claude-specific processing -> tool call ids
    5. Tool schema normalization
    6. Common parameters (safetySettings / maxOutputTokens / topK)

    Args:
        request: Original request dictionary (will not be mutated)
    Returns:
        Normalized request
    """
    result = request.copy()
    model = result.get("model", "")
    generation_config = (result.get("generationConfig") or {}).copy()
    return_thoughts = await get_return_thoughts_to_frontend()

    # ========== 0.5 Model name rewrite (panel-configurable, hot-reload) ==========
    # Applied after anti-truncation prefix stripping and before profile resolution:
    # Matching model names are replaced with the target specified in the rule (supports regex and backreferences)
    rewrite_rules = await _get_model_rewrite_rules()
    if rewrite_rules and model:
        rewritten = rewrite_model_name(model, rewrite_rules)
        if rewritten != model:
            result["model"] = rewritten
            print(f"[ANTIGRAVITY] Model name rewrite: {model} -> {rewritten}", flush=True)
        model = rewritten

    profile = resolve_profile(model)

    # ========== 0. Sensitive word filtering ==========
    # Applied before all content processing: words matching panel-configured sensitive words (case-insensitive)
    # are replaced with empty string to avoid triggering Google API endpoint sensitive word moderation
    sensitive_patterns = await _get_sensitive_word_patterns()
    if sensitive_patterns:
        if isinstance(result.get("contents"), list):
            result["contents"] = _filter_contents_sensitive_words(result["contents"], sensitive_patterns)
        if result.get("systemInstruction") is not None:
            result["systemInstruction"] = _filter_system_instruction_sensitive_words(
                result.get("systemInstruction"), sensitive_patterns
            )
        if result.get("system_instruction") is not None:
            result["system_instruction"] = _filter_system_instruction_sensitive_words(
                result.get("system_instruction"), sensitive_patterns
            )

    # Image models take independent image generation processing path
    if profile.family == "image":
        return prepare_image_generation_request(result, model)

    upstream = profile.upstream
    result["model"] = upstream

    # ========== 1. Thinking settings ==========
    _apply_thinking(profile, generation_config, return_thoughts)

    # ========== 2. contents ==========
    if "contents" in result:
        contents = _clean_contents(result["contents"], upstream)

        if profile.no_prefill:
            contents, removed = _strip_trailing_model_turns(contents)
            if removed:
                print(f"[ANTIGRAVITY] {upstream} disables prefill, removed {removed} trailing model message(s)", flush=True)

        if profile.family == "claude":
            contents = _prepare_claude_history(contents, generation_config)

        result["contents"] = _ensure_tool_call_ids(contents, upstream)

    # ========== 3. Tools ==========
    if "tools" in result:
        tools = _normalize_tools_for_internal_api(result.get("tools"))
        # Claude: functionDeclarations + parameters; Gemini: retain only parameters to avoid conflict with parametersJsonSchema
        result["tools"] = _ensure_empty_tool_schema_for_claude(tools, upstream)

    # ========== Common processing ==========
    generation_config.pop("presencePenalty", None)
    generation_config.pop("frequencyPenalty", None)
    generation_config.pop("stopSequences", None)

    result["safetySettings"] = DEFAULT_SAFETY_SETTINGS

    if generation_config:
        generation_config["maxOutputTokens"] = MAX_OUTPUT_TOKENS
        generation_config["topK"] = TOP_K
        result["generationConfig"] = generation_config

    return result
    generation_config.pop("presencePenalty", None)
    generation_config.pop("frequencyPenalty", None)
    generation_config.pop("stopSequences", None)

    result["safetySettings"] = DEFAULT_SAFETY_SETTINGS

    if generation_config:
        generation_config["maxOutputTokens"] = MAX_OUTPUT_TOKENS
        generation_config["topK"] = TOP_K
        result["generationConfig"] = generation_config

    return result