"""
Unified health check (Hi message) handling module

Provides parsing and response generation for Hi messages in OpenAI, Gemini, and Anthropic formats
"""
import time
from typing import Any, Dict, List


# ==================== Health Check Message Detection ====================

def is_health_check_request(request_data: dict, format: str = "openai") -> bool:
    """
    Check if the request is a health check request (Hi message)
    
    Args:
        request_data: Request data
        format: Request format ("openai", "gemini", or "anthropic")
        
    Returns:
        Whether it is a health check request
    """
    if format == "openai":
        # OpenAI format health check: {"messages": [{"role": "user", "content": "Hi"}]}
        messages = request_data.get("messages", [])
        if len(messages) == 1:
            msg = messages[0]
            if msg.get("role") == "user" and msg.get("content") == "Hi":
                return True
                
    elif format == "gemini":
        # Gemini format health check: {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]}
        contents = request_data.get("contents", [])
        if len(contents) == 1:
            content = contents[0]
            if (content.get("role") == "user" and 
                content.get("parts", [{}])[0].get("text") == "Hi"):
                return True
    
    elif format == "anthropic":
        # Anthropic format health check: {"messages": [{"role": "user", "content": "Hi"}]}
        messages = request_data.get("messages", [])
        if (len(messages) == 1 
            and messages[0].get("role") == "user" 
            and messages[0].get("content") == "Hi"):
            return True
    
    return False


def is_health_check_message(messages: List[Dict[str, Any]]) -> bool:
    """
    Directly check whether the message list is a health check message (Anthropic-specific)
    
    This is a convenience function for scenarios where the message list has already been extracted.
    
    Args:
        messages: Message list
        
    Returns:
        Whether it is a health check message
    """
    return (
        len(messages) == 1 
        and messages[0].get("role") == "user" 
        and messages[0].get("content") == "Hi"
    )


# ==================== Health Check Response Generation ====================

def create_health_check_response(format: str = "openai", **kwargs) -> dict:
    """
    Create a health check response
    
    Args:
        format: Response format ("openai", "gemini", or "anthropic")
        **kwargs: Format-specific additional arguments
            - model: Model name (required for anthropic format)
            - message_id: Message ID (required for anthropic format)
        
    Returns:
        Health check response dictionary
    """
    if format == "openai":
        # OpenAI format response
        return {
            "id": "healthcheck",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "healthcheck",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "API is working"
                },
                "finish_reason": "stop"
            }]
        }
    
    elif format == "gemini":
        # Gemini format response
        return {
            "candidates": [{
                "content": {
                    "parts": [{"text": "agy2api is working"}],
                    "role": "model"
                },
                "finishReason": "STOP",
                "index": 0,
            }]
        }
    
    elif format == "anthropic":
        # Anthropic format response
        model = kwargs.get("model", "claude-unknown")
        message_id = kwargs.get("message_id", "msg_healthcheck")
        return {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": str(model),
            "content": [{"type": "text", "text": "antigravity Anthropic Messages is working normally"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    
    # Return empty dictionary for unknown format
    return {}
