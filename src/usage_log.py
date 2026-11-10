"""
Usage logging service: persistence and statistics for the usage_log table.
- record_usage_log: Writes a log entry upon request completion (non-throwing, non-blocking)
  Logs enter an in-memory queue first and are flushed in batches to avoid locking SQLite
- extract_usage_tokens: Extracts usageMetadata token counts from Gemini responses / streaming chunks
"""

import asyncio
import time
import uuid
from typing import Any, Dict, Optional

import config
from log import log

from src.storage.database import get_database


def new_request_id() -> str:
    """Generate a short request ID."""
    return uuid.uuid4().hex[:16]


# ====================== Asynchronous Batch Write Queue ======================

# In-memory queue: Request hot path only performs O(1) append, background task writes in batches
_pending_logs: list = []
_queue_lock = asyncio.Lock()
# Background batch flush task (created lazily on first write)
_flush_task: Optional[asyncio.Task] = None
# Interval between batch flushes (seconds); triggers immediately when buffer is full
_FLUSH_INTERVAL = 2.0
# Maximum buffer size (flushes immediately when exceeded to prevent memory growth under spikes)
_MAX_PENDING = 500

_FLUSH_LOOP_STOP = False


async def _flush_pending_logs() -> None:
    """Flush pending logs to database in batch (transactional, individual failures do not affect batch)."""
    global _pending_logs
    async with _queue_lock:
        batch = _pending_logs
        _pending_logs = []
    if not batch:
        return

    database = await get_database()
    for entry in batch:
        try:
            await database.insert_usage_log(entry)
        except Exception as e:
            log.debug(f"Failed to write usage log (non-fatal): {e}")


async def _flush_loop() -> None:
    """Background periodic batch flush loop."""
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        try:
            await _flush_pending_logs()
        except Exception as e:
            log.debug(f"Exception during usage log batch flush: {e}")


def _ensure_flush_loop() -> None:
    """Ensure background flush task is active (lazy startup, attached to event loop lifecycle)."""
    global _flush_task
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.get_event_loop().create_task(_flush_loop())


async def flush_usage_logs() -> None:
    """Immediately flush all pending logs to database (called on shutdown to avoid data loss)."""
    await _flush_pending_logs()


def _enqueue_log(entry: Dict[str, Any]) -> None:
    """Enqueue a log entry; triggers batch flush if buffer limit is reached."""
    global _pending_logs
    _pending_logs.append(entry)
    if len(_pending_logs) >= _MAX_PENDING:
        try:
            # Already inside event loop: schedule immediate flush (non-blocking)
            asyncio.get_event_loop().create_task(_flush_pending_logs())
        except Exception:
            pass


async def _write_log(entry: Dict[str, Any]) -> None:
    """Log write entry point: enters batch queue after checking configuration toggle."""
    try:
        if not await config.get_usage_log_enabled():
            return
        _ensure_flush_loop()
        _enqueue_log({**entry, "created_at": entry.get("created_at") or time.time()})
    except Exception as e:
        log.debug(f"Failed to enqueue usage log (non-fatal): {e}")


def extract_usage_tokens(source: Any) -> Dict[str, int]:
    """
    Extract token usage metrics from Gemini response structures.

    Compatible with:
    - Non-streaming: {"usageMetadata": {...}} or {"response": {"usageMetadata": {...}}}
    - Streaming chunks: usageMetadata inside SSE data string (usually present in final chunk)

    Gemini semantics:
    - promptTokenCount includes cached content (cachedContentTokenCount)
    - completion = candidatesTokenCount + thoughtsTokenCount

    Also compatible with newer Interactions format:
    - total_input_tokens / total_output_tokens / total_thought_tokens / total_cached_tokens
    """
    prompt_tokens = completion_tokens = total_tokens = cached_tokens = 0

    if isinstance(source, (str, bytes)):
        # Streaming SSE chunk: parse JSON to extract usageMetadata
        try:
            text = source.decode("utf-8") if isinstance(source, bytes) else source
            if text.startswith("data: "):
                text = text[len("data: "):].strip()
            if not text or text == "[DONE]":
                return {}
            source = __import__("json").loads(text)
        except Exception:
            return {}

    if not isinstance(source, dict):
        return {}

    metadata = source.get("usageMetadata")
    if not isinstance(metadata, dict):
        # Unwrap outer {"response": {...}} wrapper if present
        response = source.get("response")
        if isinstance(response, dict):
            metadata = response.get("usageMetadata")
    if not isinstance(metadata, dict):
        return {}

    def _first_int(*keys: str) -> int:
        for key in keys:
            value = metadata.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    prompt_tokens = _first_int("promptTokenCount", "total_input_tokens", "input_tokens")
    reasoning_tokens = _first_int("thoughtsTokenCount", "total_thought_tokens")
    candidates_tokens = _first_int("candidatesTokenCount", "total_output_tokens")
    completion_tokens = candidates_tokens + reasoning_tokens
    total_tokens = _first_int("totalTokenCount", "total_tokens") or (prompt_tokens + completion_tokens)

    # Cached tokens: legacy cachedContentTokenCount / modern total_cached_tokens, and Anthropic details
    cached_tokens = _first_int(
        "cachedContentTokenCount",
        "cached_content_token_count",
        "total_cached_tokens",
        "cached_tokens",
        "cache_read_input_tokens",
    )
    if not cached_tokens:
        for details_key in ("prompt_tokens_details", "input_tokens_details"):
            details = metadata.get(details_key)
            if isinstance(details, dict):
                value = details.get("cached_tokens")
                if value is not None:
                    try:
                        cached_tokens = int(value)
                    except (TypeError, ValueError):
                        cached_tokens = 0
                    break

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
    }


async def record_usage_log(entry: Dict[str, Any]) -> None:
    """
    Write a request usage log entry. Non-blocking, non-fatal on errors.

    Logs enter in-memory queue first for batch writing.

    Required fields: model_name, success
    Optional fields: api_format, client_ip, request_id, credential_email, is_stream,
                     status_code, use_time, prompt_tokens, completion_tokens, total_tokens, cached_tokens, error_message,
                     request_log_file (disk relative path index, empty = none)
    """
    await _write_log(entry)


async def clear_usage_logs(days: int) -> int:
    """Purge logs older than N days, returning deleted count."""
    await flush_usage_logs()
    database = await get_database()
    cutoff = time.time() - days * 86400
    return await database.delete_usage_logs_before(cutoff)
