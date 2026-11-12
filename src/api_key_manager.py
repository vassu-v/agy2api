"""
API Key Manager - Handles multi-key authentication, caching, rate limiting, and quotas.
Designed for high concurrency with zero DB overhead on hot paths.
"""

import asyncio
import collections
from datetime import datetime, timezone
import hashlib
import math
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from log import log
from src.storage.database import get_database


# ====================== Global Caches & State ======================

# In-memory mapping: key_hash (SHA-256) -> key dict
_keys_by_hash: Dict[str, Dict[str, Any]] = {}
_keys_initialized: bool = False
_keys_lock = asyncio.Lock()

# Sliding window for RPM: key_hash -> deque of monotonic timestamps within last 60 seconds
_rpm_windows: Dict[str, collections.deque] = {}

# Daily request counters: key_hash -> {"date": utc_date_str, "count": int}
_daily_counters: Dict[str, Dict[str, Any]] = {}

# Asynchronous batch usage accumulator: key_hash -> {"requests": int, "tokens": int}
_pending_usage: Dict[str, Dict[str, int]] = {}
_usage_lock = asyncio.Lock()

# Background flush task
_flush_task: Optional[asyncio.Task] = None
_FLUSH_INTERVAL = 5.0


def hash_key(key: str) -> str:
    """Compute SHA-256 hash of API key string."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def generate_secure_key(prefix: str = "sk-agy-") -> str:
    """Generate a high-entropy cryptographically secure API key (256-bit entropy)."""
    random_part = secrets.token_urlsafe(32)
    return f"{prefix}{random_part}"


def mask_key(key: str) -> str:
    """Format key for display with masking (e.g. sk-agy-••••••••4a2f)."""
    if not key:
        return "••••••••"
    if len(key) <= 12:
        return "••••••••"
    return f"{key[:8]}••••••••{key[-4:]}"


# ====================== Lifecycle & Cache Management ======================

async def init_api_keys() -> None:
    """Load all API keys into memory on server startup."""
    global _keys_by_hash, _keys_initialized
    async with _keys_lock:
        try:
            db = await get_database()
            keys_list = await db.list_api_keys()
            _keys_by_hash = {k["key_hash"]: k for k in keys_list}
            _keys_initialized = True
            log.info(f"API Key Manager initialized ({len(_keys_by_hash)} keys loaded)")
        except Exception as e:
            log.error(f"Failed to load API keys cache: {e}")
            _keys_by_hash = {}
            _keys_initialized = True


async def reload_api_keys() -> None:
    """Refresh in-memory API keys cache from database while preserving unflushed deltas."""
    global _keys_by_hash, _rpm_windows, _daily_counters
    async with _keys_lock:
        try:
            db = await get_database()
            keys_list = await db.list_api_keys()
            new_cache = {k["key_hash"]: k for k in keys_list}

            # Merge pending uncommitted usage deltas so cache counters don't jump backwards
            async with _usage_lock:
                for kh, deltas in _pending_usage.items():
                    if kh in new_cache:
                        new_cache[kh]["total_requests"] += deltas.get("requests", 0)
                        new_cache[kh]["total_tokens"] += deltas.get("tokens", 0)

            _keys_by_hash = new_cache

            # Prune limiter state for keys that have been deleted
            active_hashes = set(new_cache.keys())
            for kh in list(_rpm_windows.keys()):
                if kh not in active_hashes:
                    _rpm_windows.pop(kh, None)
            for kh in list(_daily_counters.keys()):
                if kh not in active_hashes:
                    _daily_counters.pop(kh, None)

            log.debug(f"API Key Manager reloaded ({len(_keys_by_hash)} keys active)")
        except Exception as e:
            log.error(f"Failed to reload API keys cache: {e}")


# ====================== Background Usage Flush ======================

async def _flush_pending_usage() -> None:
    """Flush accumulated request and token deltas to database with per-key idempotency."""
    global _pending_usage
    async with _usage_lock:
        if not _pending_usage:
            return
        to_flush, _pending_usage = _pending_usage, {}

    db = await get_database()
    committed_keys = set()
    try:
        for kh, deltas in list(to_flush.items()):
            req_delta = deltas.get("requests", 0)
            token_delta = deltas.get("tokens", 0)
            if req_delta > 0 or token_delta > 0:
                await db.increment_api_key_usage(kh, req_delta, token_delta)
            committed_keys.add(kh)
    except Exception as e:
        log.error(f"Failed to flush API key usage deltas: {e}")
        # Re-merge ONLY uncommitted deltas to prevent double-counting
        async with _usage_lock:
            for kh, deltas in to_flush.items():
                if kh not in committed_keys:
                    if kh not in _pending_usage:
                        _pending_usage[kh] = {"requests": 0, "tokens": 0}
                    _pending_usage[kh]["requests"] += deltas.get("requests", 0)
                    _pending_usage[kh]["tokens"] += deltas.get("tokens", 0)


async def _usage_flush_loop() -> None:
    """Periodic background flush loop."""
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        try:
            await _flush_pending_usage()
        except Exception as e:
            log.debug(f"Exception during API key usage flush: {e}")


def ensure_usage_flush_loop() -> None:
    """Ensure background flush loop is active."""
    global _flush_task
    if _flush_task is None or _flush_task.done():
        try:
            _flush_task = asyncio.get_running_loop().create_task(_usage_flush_loop())
        except RuntimeError:
            pass


async def flush_api_key_usage() -> None:
    """Immediately flush all pending usage deltas (called on shutdown)."""
    await _flush_pending_usage()


def record_api_key_usage(key_hash: str, tokens: int = 0) -> None:
    """
    Record request and token consumption asynchronously without blocking.
    Updates in-memory counters immediately, and enqueues DB flush.
    """
    ensure_usage_flush_loop()
    tokens = max(0, tokens)

    # 1. Update in-memory cache directly for immediate limit enforcement
    key_info = _keys_by_hash.get(key_hash)
    if key_info:
        key_info["total_requests"] = key_info.get("total_requests", 0) + 1
        key_info["total_tokens"] = key_info.get("total_tokens", 0) + tokens
        key_info["last_used_at"] = time.time()

    # 2. Accumulate delta for batched DB update
    if key_hash not in _pending_usage:
        _pending_usage[key_hash] = {"requests": 0, "tokens": 0}
    _pending_usage[key_hash]["requests"] += 1
    _pending_usage[key_hash]["tokens"] += tokens


# ====================== Key Verification & Rate Limiting ======================

def get_key_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Look up key in memory cache by raw token."""
    kh = hash_key(token)
    return _keys_by_hash.get(kh)


def validate_api_key(token: str, model_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Validate an incoming API key token against active status, expiry, rate limits, and quotas.
    Fast synchronous execution with atomic two-phase rate check (check all, then commit).

    Raises:
        HTTPException(401): If key is invalid or unknown.
        HTTPException(403): If key is disabled, expired, or model is restricted.
        HTTPException(429): If RPM limit, daily limit, or token quota is exceeded.
    """
    kh = hash_key(token)
    key_info = _keys_by_hash.get(kh)

    if not key_info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 1. Active status check
    if not key_info.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key has been disabled",
        )

    # 2. Expiration check
    expires_at = key_info.get("expires_at")
    now_wall = time.time()
    if expires_at and now_wall >= expires_at:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key has expired",
        )

    # 3. Lifetime Token Quota check
    token_limit = key_info.get("token_limit", 0)
    if token_limit > 0 and key_info.get("total_tokens", 0) >= token_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Token quota exceeded: limit of {token_limit:,} tokens reached",
        )

    # 4. Model Whitelist check (if model_name provided)
    allowed_models = key_info.get("models_allowed") or []
    if allowed_models and model_name:
        from src.utils import get_base_model_from_feature_model
        base_model = get_base_model_from_feature_model(model_name).lower()
        allowed_normalized = [m.lower().strip() for m in allowed_models if m.strip()]
        if allowed_normalized and base_model not in allowed_normalized and model_name.lower() not in allowed_normalized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Model '{model_name}' is not permitted for this API key",
            )

    # 5. Phase 1: Test Rate Limits without mutating state
    rpm_limit = key_info.get("rpm_limit", 0)
    now_mono = time.monotonic()
    rpm_queue = None
    if rpm_limit > 0:
        rpm_queue = _rpm_windows.setdefault(kh, collections.deque())
        cutoff = now_mono - 60.0
        while rpm_queue and rpm_queue[0] < cutoff:
            rpm_queue.popleft()

        if len(rpm_queue) >= rpm_limit:
            oldest = rpm_queue[0]
            retry_after = max(1, math.ceil(60.0 - (now_mono - oldest)))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: maximum {rpm_limit} requests per minute",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit-RPM": str(rpm_limit),
                    "X-RateLimit-Remaining-RPM": "0",
                    "X-RateLimit-Reset-RPM": str(retry_after),
                },
            )

    daily_limit = key_info.get("daily_limit", 0)
    daily_state = None
    if daily_limit > 0:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_state = _daily_counters.setdefault(kh, {"date": today_utc, "count": 0})
        if daily_state.get("date") != today_utc:
            daily_state["date"] = today_utc
            daily_state["count"] = 0

        if daily_state["count"] >= daily_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily request limit exceeded: maximum {daily_limit} requests per day",
                headers={
                    "X-RateLimit-Limit-Daily": str(daily_limit),
                    "X-RateLimit-Remaining-Daily": "0",
                },
            )

    # 6. Phase 2: All checks passed - commit increments atomically
    if rpm_queue is not None:
        rpm_queue.append(now_mono)
    if daily_state is not None:
        daily_state["count"] += 1

    return key_info
