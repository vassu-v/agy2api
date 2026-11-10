"""
thoughtSignature common processing module.

Provides unified thoughtSignature encoding/decoding functionality to preserve signature information
within tool call IDs. This allows signatures to persist across client round-trips even if clients drop custom fields.
"""

from typing import Any, Mapping, Optional, Tuple

# Separator for embedding thoughtSignature within tool call IDs
# This allows signatures to persist across client round-trips even if clients drop custom fields
THOUGHT_SIGNATURE_SEPARATOR = "__thought__"
SKIP_THOUGHT_SIGNATURE_VALIDATOR = "skip_thought_signature_validator"
SKIP_THOUGHT_SIGNATURE_PLACEHOLDER_TEXT = "..."


def is_internal_placeholder_text(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return text.strip() in (SKIP_THOUGHT_SIGNATURE_PLACEHOLDER_TEXT, "…")


def is_skip_thought_signature_placeholder(part: Mapping[str, Any]) -> bool:
    """Return True for the internal placeholder that should not reach clients."""
    if not isinstance(part, Mapping):
        return False
    if part.get("thoughtSignature") != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
        return False
    if "functionCall" in part or "function_call" in part or "functionResponse" in part:
        return False
    return is_internal_placeholder_text(part.get("text"))


def encode_tool_id_with_signature(tool_id: str, signature: Optional[str]) -> str:
    """
    Encode thoughtSignature into the tool call ID for round-trip preservation.

    Args:
        tool_id: Original tool call ID
        signature: thoughtSignature (optional)

    Returns:
        Encoded tool call ID

    Examples:
        >>> encode_tool_id_with_signature("call_123", "abc")
        'call_123__thought__abc'
        >>> encode_tool_id_with_signature("call_123", None)
        'call_123'
    """
    if not signature:
        return tool_id
    return f"{tool_id}{THOUGHT_SIGNATURE_SEPARATOR}{signature}"


def decode_tool_id_and_signature(encoded_id: str) -> Tuple[str, Optional[str]]:
    """
    Extract original tool ID and thoughtSignature from encoded ID.

    Args:
        encoded_id: Encoded tool call ID

    Returns:
        (original_tool_id, thoughtSignature) tuple

    Examples:
        >>> decode_tool_id_and_signature("call_123__thought__abc")
        ('call_123', 'abc')
        >>> decode_tool_id_and_signature("call_123")
        ('call_123', None)
    """
    if not encoded_id or THOUGHT_SIGNATURE_SEPARATOR not in encoded_id:
        return encoded_id, None
    parts = encoded_id.split(THOUGHT_SIGNATURE_SEPARATOR, 1)
    return parts[0], parts[1] if len(parts) == 2 else None
