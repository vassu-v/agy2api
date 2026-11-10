from typing import Any, Dict

from src.converter.thoughtSignature_fix import (
    is_internal_placeholder_text,
    is_skip_thought_signature_placeholder,
)


def extract_content_and_reasoning(parts: list) -> tuple:
    """Extract content and reasoning content from Gemini response parts.

    Args:
        parts: List of parts in the Gemini response

    Returns:
        (content, reasoning_content, images): Tuple of text content, reasoning content, and image data
        - content: Text content string
        - reasoning_content: Reasoning content string
        - images: List of image data dictionaries, each in the format:
          {
              "type": "image_url",
              "image_url": {
                  "url": "data:{mime_type};base64,{base64_data}"
              }
          }
    """
    content = ""
    reasoning_content = ""
    images = []

    for part in parts:
        if is_skip_thought_signature_placeholder(part):
            continue

        # Extract text content
        text = part.get("text", "")
        if is_internal_placeholder_text(text):
            continue
        if text:
            if part.get("thought", False):
                reasoning_content += text
            else:
                content += text

        # Extract image data
        if "inlineData" in part:
            inline_data = part["inlineData"]
            mime_type = inline_data.get("mimeType", "image/png")
            base64_data = inline_data.get("data", "")
            images.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_data}"
                }
            })

    return content, reasoning_content, images


async def merge_system_messages(request_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process system messages in the request body:
    - Merge top-level system parameter (Anthropic format) and leading consecutive system messages into systemInstruction
    - Convert intermediate system messages to user messages (preserving order)

    Args:
        request_body: Request body in OpenAI or Claude format, containing messages field

    Returns:
        Processed request body
    """
    # Handle top-level system parameter in Anthropic format
    # Anthropic API spec: system is a top-level parameter, not inside messages
    system_content = request_body.get("system")
    if system_content:
        system_parts = []

        if isinstance(system_content, str):
            if system_content.strip():
                system_parts.append({"text": system_content})
        elif isinstance(system_content, list):
            # system can be a list containing multiple blocks
            for item in system_content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text", "").strip():
                        system_parts.append({"text": item["text"]})
                elif isinstance(item, str) and item.strip():
                    system_parts.append({"text": item})

        if system_parts:
            request_body = request_body.copy()
            request_body.pop("system", None)
            request_body["systemInstruction"] = {"parts": system_parts}

    messages = request_body.get("messages", [])
    if not messages:
        return request_body

    # Extract leading consecutive system messages and merge into systemInstruction
    system_parts = []

    # If systemInstruction was already created from top-level system parameter, get existing parts
    if "systemInstruction" in request_body:
        existing_instruction = request_body.get("systemInstruction", {})
        if isinstance(existing_instruction, dict):
            system_parts = existing_instruction.get("parts", []).copy()

    remaining_messages = []
    collecting_system = True

    # developer role has the same semantics as system (newer OpenAI instruction role), upstream only supports user/model
    instruction_roles = ("system", "developer")

    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")

        if role in instruction_roles and collecting_system:
            # Extract text content from system message
            if isinstance(content, str):
                if content.strip():
                    system_parts.append({"text": content})
            elif isinstance(content, list):
                # Handle list-formatted content
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text" and item.get("text", "").strip():
                            system_parts.append({"text": item["text"]})
                    elif isinstance(item, str) and item.strip():
                        system_parts.append({"text": item})
        else:
            # Encountered non-system message, stop collecting
            collecting_system = False
            if role in instruction_roles:
                # Convert subsequent system messages to user messages
                converted_message = message.copy()
                converted_message["role"] = "user"
                remaining_messages.append(converted_message)
            else:
                remaining_messages.append(message)

    # If no systemInstruction content was collected:
    # Only return original request body if messages did not change;
    # If there are intermediate system messages converted to user, return updated messages
    if not system_parts:
        if remaining_messages == messages:
            return request_body
        result = request_body.copy()
        result["messages"] = remaining_messages
        return result

    # Build new request body
    result = request_body.copy()

    # Add or update systemInstruction
    result["systemInstruction"] = {"parts": system_parts}

    # Update messages list (remove processed system messages)
    result["messages"] = remaining_messages

    return result
