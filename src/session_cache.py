"""
Session credential cache (credential affinity).
Binds the same session (stable key derived from request content) to the same credential to hit upstream prompt cache.

Dual backends:
- Redis (optional): Enabled when REDIS_URL is set, multi-instance shared binding
- In-memory (default): Automatically used when Redis is absent, in-process dict + expiration eviction

Behavior:
- get: Renews TTL on cache hit (keeps active sessions bound, evicts idle sessions)
- set: Writes/refreshes binding after each successful request
- Redis failures log warnings and fall back to in-memory store without breaking main flow
"""

import asyncio
import hashlib
import json
import time
from typing import Any, Dict, Optional

from log import log

# Binding TTL (seconds): unbind session after idle period
SESSION_BINDING_TTL = 3600

_KEY_PREFIX = "agy2api:sess:"

# Redis client (None = disabled, using in-memory store)
_redis_client = None

# In-memory store: {session_key: (email, expire_at)}
_memory_store: Dict[str, tuple] = {}
_memory_lock = asyncio.Lock()

# In-memory capacity limit (prevents unbounded memory growth); evicts expired then oldest entries
_MEMORY_MAX_ENTRIES = 50000


def _now() -> float:
    return time.time()


def _get_client():
    """Return initialized Redis client; None when disabled."""
    return _redis_client


async def init_redis(redis_url: str) -> bool:
    """Initialize Redis connection. Returns False on failure without raising exceptions."""
    global _redis_client
    if not redis_url:
        return False
    try:
        import redis.asyncio as redis_aiolib

        client = redis_aiolib.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
        )
        await client.ping()
        _redis_client = client
        log.info("Session credential cache connected to Redis")
        return True
    except Exception as e:
        log.warning(f"Redis connection failed, falling back to in-memory store: {e}")
        _redis_client = None
        return False


async def close() -> None:
    """Close Redis connection (called on application shutdown)."""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
        _redis_client = None
    _memory_store.clear()


# ====================== Session Key Derivation ======================

def derive_session_key(model_name: str, gemini_request: Dict[str, Any]) -> Optional[str]:
    """
    Derive session key from normalized Gemini request:
    hash(model + systemInstruction or first user message)

    Must be called after format conversion (openai2gemini / anthropic2gemini / native gemini
    are all normalized to Gemini format), ensuring identical content yields identical key.

    Returns:
        "auto:<hash>" or None (when key cannot be derived)
    """
    if not isinstance(gemini_request, dict):
        return None

    model_part = model_name if isinstance(model_name, str) else ""

    # Prioritize systemInstruction (stable anchor for conversation)
    anchor = None
    system_instruction = gemini_request.get("systemInstruction")
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts")
        if isinstance(parts, list) and parts:
            anchor = _stable_json(parts)

    # When system is absent, use first user message parts
    if anchor is None:
        contents = gemini_request.get("contents")
        if isinstance(contents, list):
            for content in contents:
                if isinstance(content, dict) and content.get("role") == "user":
                    parts = content.get("parts")
                    if isinstance(parts, list) and parts:
                        anchor = _stable_json(parts)
                        break

    if anchor is None:
        return None

    digest = hashlib.sha256(f"{model_part}|{anchor}".encode("utf-8")).hexdigest()
    return f"auto:{digest[:32]}"


def _stable_json(value: Any) -> Optional[str]:
    """Deterministic JSON serialization (sorted keys), returns None on failure."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


# ====================== Binding Read/Write ======================

async def get_session_binding(session_key: str) -> Optional[str]:
    """Lookup bound credential email; renews TTL on hit. Returns None if unbound or on error."""
    if not session_key:
        return None

    client = _get_client()
    if client is not None:
        redis_key = f"{_KEY_PREFIX}{session_key}"
        try:
            raw = await client.get(redis_key)
            if raw is None:
                return None
            await client.expire(redis_key, SESSION_BINDING_TTL)
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception as e:
            log.warning(f"Redis get session binding failed: {e}")
            # Fall back to in-memory store on Redis failure

    return await _memory_get(session_key)


async def set_session_binding(session_key: str, email: str) -> None:
    """Save or refresh session binding (called after successful request). Non-fatal on failure."""
    if not session_key or not email:
        return

    client = _get_client()
    if client is not None:
        try:
            await client.setex(
                f"{_KEY_PREFIX}{session_key}", SESSION_BINDING_TTL, email
            )
        except Exception as e:
            log.warning(f"Redis set session binding failed: {e}")

    await _memory_set(session_key, email)


async def delete_session_binding(session_key: str) -> None:
    """Delete session binding (e.g. when bound credential becomes unavailable)."""
    if not session_key:
        return

    client = _get_client()
    if client is not None:
        try:
            await client.delete(f"{_KEY_PREFIX}{session_key}")
        except Exception as e:
            log.warning(f"Redis delete session binding failed: {e}")

    async with _memory_lock:
        _memory_store.pop(session_key, None)


# ====================== In-Memory Store ======================

async def _memory_get(session_key: str) -> Optional[str]:
    async with _memory_lock:
        entry = _memory_store.get(session_key)
        if not entry:
            return None
        email, expire_at = entry
        if _now() >= expire_at:
            _memory_store.pop(session_key, None)
            return None
        # Renew on hit
        _memory_store[session_key] = (email, _now() + SESSION_BINDING_TTL)
        return email


async def _memory_set(session_key: str, email: str) -> None:
    async with _memory_lock:
        # Capacity control: evict expired entries first if limit reached
        if len(_memory_store) >= _MEMORY_MAX_ENTRIES:
            _cleanup_expired_locked()
        # If still over limit, drop oldest half (sorted by expiration time)
        if len(_memory_store) >= _MEMORY_MAX_ENTRIES:
            sorted_items = sorted(_memory_store.items(), key=lambda kv: kv[1][1])
            for k, _ in sorted_items[: len(sorted_items) // 2]:
                _memory_store.pop(k, None)
        _memory_store[session_key] = (email, _now() + SESSION_BINDING_TTL)


def _cleanup_expired_locked() -> None:
    """Evict expired entries (must be called with lock held)."""
    now = _now()
    expired = [k for k, (_, exp) in _memory_store.items() if now >= exp]
    for k in expired:
        _memory_store.pop(k, None)


def memory_store_size() -> int:
    """In-memory store entry count (diagnostic)."""
    return len(_memory_store)
