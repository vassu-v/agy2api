"""Simple token estimation, fast heuristic without requiring exact precision."""
from __future__ import annotations

from typing import Any, Dict


def estimate_input_tokens(payload: Dict[str, Any]) -> int:
    """Roughly estimate token count: characters / 4 + fixed tokens per image."""
    total_chars = 0
    image_count = 0

    # Count all text characters
    def count_str(obj: Any) -> None:
        nonlocal total_chars, image_count
        if isinstance(obj, str):
            total_chars += len(obj)
        elif isinstance(obj, dict):
            # Detect images
            if obj.get("type") == "image" or "inlineData" in obj:
                image_count += 1
            for v in obj.values():
                count_str(v)
        elif isinstance(obj, list):
            for item in obj:
                count_str(item)

    count_str(payload)

    # Heuristic estimate: characters / 4 + 300 tokens per image
    return max(1, total_chars // 4 + image_count * 300)
