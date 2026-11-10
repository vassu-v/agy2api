"""
Generic HTTP client module.
Provides unified client configuration and methods for all modules using httpx.
Keeps general applicability without coupling to specific business logic.

- Shared AsyncClient (cached by proxy configuration fingerprint), reuses TCP/TLS connections,
  avoiding per-request handshake overhead.
- Fingerprint updates dynamically when proxy settings change; stale clients are closed lazily.
- Per-request parameters like timeout are passed on invocation.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
import json
from fastapi import Response

from config import get_proxy_config
from log import log

# ====================== Shared Client ======================

# (proxy_fingerprint, AsyncClient) list; normally at most 2 entries (with proxy / without proxy)
_shared_clients: List[Tuple[str, httpx.AsyncClient]] = []
_shared_clients_lock = asyncio.Lock()


def _client_fingerprint(proxy_url: Optional[str]) -> str:
    """Client fingerprint: different proxy URL cannot reuse the same client."""
    return proxy_url or ""


async def _build_client(proxy_url: Optional[str]) -> httpx.AsyncClient:
    """Build client according to current proxy configuration."""
    client_kwargs: Dict[str, Any] = {"timeout": None}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**client_kwargs)


async def _safe_aclose(client: httpx.AsyncClient) -> None:
    """Safely close client, logging warning on failure."""
    try:
        await client.aclose()
    except Exception as e:
        log.warning(f"Failed to close old httpx client: {e}")


async def _get_shared_client(**kwargs) -> httpx.AsyncClient:
    """
    Get shared AsyncClient (cached and reused by proxy fingerprint).

    Callers must not close this client (lifecycle managed by module,
    closed via close_shared_clients on web.py shutdown).
    """
    proxy_url = await get_proxy_config()
    fingerprint = _client_fingerprint(proxy_url)

    # Fast path: matching fingerprint reused directly (lock-free read)
    for fp, client in _shared_clients:
        if fp == fingerprint:
            return client

    # Slow path: create or rebuild client
    async with _shared_clients_lock:
        # Double check (might have been created concurrently by another request)
        for fp, client in _shared_clients:
            if fp == fingerprint:
                return client

        # Proxy hot reload: remove stale clients and close asynchronously
        stale: List[httpx.AsyncClient] = []
        kept: List[Tuple[str, httpx.AsyncClient]] = []
        for fp, client in _shared_clients:
            if fp == fingerprint:
                kept.append((fp, client))
            else:
                stale.append(client)
        _shared_clients[:] = kept

        new_client = await _build_client(proxy_url)
        _shared_clients.append((fingerprint, new_client))

        # Lazily close stale clients without blocking current request
        for old in stale:
            try:
                asyncio.create_task(_safe_aclose(old))
            except Exception as e:
                log.warning(f"Failed to schedule old client closure: {e}")

        return new_client


async def close_shared_clients() -> None:
    """Close all shared clients (called on application shutdown)."""
    async with _shared_clients_lock:
        clients = [c for _, c in _shared_clients]
        _shared_clients.clear()
    for client in clients:
        await _safe_aclose(client)


# ====================== Client Retrieval (Legacy Interface Compatibility) ======================

@asynccontextmanager
async def get_client(
    timeout: float = 30.0, **kwargs
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Get configured async HTTP client (shared and reused)."""
    yield await _get_shared_client()


@asynccontextmanager
async def get_streaming_client(
    timeout: float = None, **kwargs
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Get HTTP client for streaming requests (shared and reused)."""
    yield await _get_shared_client()


# ====================== Async Request Methods ======================

async def get_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> httpx.Response:
    """Generic async GET request."""
    client = await _get_shared_client()
    return await client.get(url, headers=headers, timeout=timeout, **kwargs)


async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 900.0,
    **kwargs,
) -> httpx.Response:
    """Generic async POST request."""
    client = await _get_shared_client()
    return await client.post(
        url, data=data, json=json, headers=headers, timeout=timeout, **kwargs
    )


# Debug flag: when True, all streaming requests return 429
_MOCK_STREAM_429 = False

async def stream_post_async(
    url: str,
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    **kwargs,
):
    """Streaming async POST request."""
    if _MOCK_STREAM_429:
        log.warning(f"[MOCK] stream_post_async: returning simulated 429 error")
        yield Response(
            content=json.dumps({"error": {"code": 429, "message": "mock rate limit", "status": "RESOURCE_EXHAUSTED"}}),
            status_code=429,
        )
        return

    client = await _get_shared_client()
    async with client.stream(
        "POST", url, json=body, headers=headers, **kwargs
    ) as r:
        # Return error response directly
        if r.status_code != 200:
            yield Response(await r.aread(), r.status_code, dict(r.headers))
            return

        # If native=True, yield raw bytes stream directly
        if native:
            async for chunk in r.aiter_bytes():
                yield chunk
        else:
            # Yield string lines via aiter_lines
            async for line in r.aiter_lines():
                yield line
