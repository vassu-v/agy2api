"""
Main Web Integration - Integrates all routers and modules
Aggregates routers and launches main server
"""

import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from log import log

# Import managers and utilities
# credential_manager has been refactored into a stateless functional module (no init/close required)

# Import all routers
from src.router.antigravity.openai import router as antigravity_openai_router
from src.router.antigravity.gemini import router as antigravity_gemini_router
from src.router.antigravity.anthropic import router as antigravity_anthropic_router
from src.router.antigravity.model_list import router as antigravity_model_list_router
from src.panel import router as panel_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan management."""
    log.info("Starting AGY2API main service")

    # Initialize configuration cache (first priority)
    try:
        import config
        await config.init_config()
        log.info("Configuration cache initialized successfully")
    except Exception as e:
        log.error(f"Failed to initialize configuration cache: {e}")

    # Warm up database connection (expose config issues early)
    try:
        from src.storage.database import get_database
        await get_database()
        log.info("Database connection ready")
    except Exception as e:
        log.error(f"Failed to initialize database: {e}")

    # Initialize session credential cache (Redis optional, falls back to in-memory)
    try:
        import os as os_module
        from src import session_cache

        redis_url = os_module.getenv("REDIS_URL", "")
        if redis_url:
            await session_cache.init_redis(redis_url)
        else:
            log.info("REDIS_URL not configured, using in-memory backend for session cache")
    except Exception as e:
        log.warning(f"Failed to initialize session cache (falling back to in-memory): {e}")

    # OAuth callback server starts on demand when needed

    yield

    # Clean up resources
    log.info("Shutting down AGY2API main service")

    # Close credential manager and database
    try:
        from src.usage_log import flush_usage_logs
        await flush_usage_logs()
        log.info("Request logs flushed to database")
    except Exception as e:
        log.error(f"Error flushing request logs: {e}")

    try:
        from src.storage.database import close_database
        await close_database()
        log.info("Database closed")
    except Exception as e:
        log.error(f"Error closing database: {e}")

    # Close shared httpx clients (release connection pool)
    try:
        from src.httpx_client import close_shared_clients
        await close_shared_clients()
        log.info("HTTP client closed")
    except Exception as e:
        log.error(f"Error closing HTTP client: {e}")

    # Close session credential cache
    try:
        from src import session_cache
        await session_cache.close()
    except Exception as e:
        log.debug(f"Error closing session cache: {e}")

    log.info("AGY2API main service stopped")


class IPWhitelistMiddleware:
    """
    IP Whitelist Middleware (pure ASGI, covers panel and all API routes)

    When ip_whitelist is non-empty, only whitelisted client IPs are allowed,
    all other requests return 403. When empty, no restriction is enforced.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        import config

        try:
            enabled, rules = await config.get_ip_whitelist_rules()
        except Exception as e:
            # Allow request through on config read error to avoid locking out all traffic
            log.error(f"Failed to read IP whitelist config, request allowed: {e}")
            await self.app(scope, receive, send)
            return

        if enabled:
            client_ip = (scope.get("client") or (None, None))[0]
            if not client_ip or not config.ip_in_whitelist(client_ip, rules):
                log.warning(f"IP whitelist blocked request: {client_ip} -> {scope.get('path', '')}")
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
                accept = headers.get("accept", "")
                response: JSONResponse | PlainTextResponse
                if "application/json" in accept or scope["path"].startswith(("/v1/", "/config/")):
                    response = JSONResponse(
                        status_code=403,
                        content={"error": {"message": "IP not allowed", "code": 403}},
                    )
                else:
                    response = PlainTextResponse("403 Forbidden: IP not allowed", status_code=403)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class PathPrefixMiddleware:
    """
    Path prefix stripping middleware (pure ASGI)

    When reverse proxy retains a path prefix (e.g. location /AGY2API/ with proxy_pass without URI),
    requests arrive as /AGY2API/xxx. When PATH_PREFIX is configured, this middleware strips
    the prefix before route matching.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope["path"] = path[len(self.prefix):] or "/"
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes) and raw_path.startswith(
                    self.prefix.encode("ascii")
                ):
                    scope["raw_path"] = raw_path[len(self.prefix):] or b"/"
        await self.app(scope, receive, send)


# Create FastAPI application
app = FastAPI(
    title="AGY2API",
    description="Antigravity API proxy with OpenAI / Gemini / Claude compatibility",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# IP Whitelist middleware (runs before CORS and all routes)
app.add_middleware(IPWhitelistMiddleware)

# Path prefix stripping middleware (enabled when PATH_PREFIX is set)
path_prefix = os.environ.get("PATH_PREFIX", "").rstrip("/")
if path_prefix:
    app.add_middleware(PathPrefixMiddleware, prefix=path_prefix)
    log.info(f"Path prefix stripping enabled: {path_prefix}")

# Mount routers
# Antigravity router - handles OpenAI format requests and converts to Antigravity API
app.include_router(antigravity_openai_router, prefix="", tags=["Antigravity OpenAI API"])

# Antigravity router - handles Gemini format requests and converts to Antigravity API
app.include_router(antigravity_gemini_router, prefix="", tags=["Antigravity Gemini API"])

# Antigravity model list router - handles Gemini format model list requests
app.include_router(antigravity_model_list_router, prefix="", tags=["Antigravity Model List"])

# Antigravity Anthropic Messages router - Anthropic Messages format compatibility
app.include_router(antigravity_anthropic_router, prefix="", tags=["Antigravity Anthropic Messages"])

# Panel router - contains authentication, credential management, and control panel features
app.include_router(panel_router, prefix="", tags=["Panel Interface"])

# Static files route - serves files under docs directory (skipped if directory does not exist)
if os.path.isdir("docs"):
    app.mount("/docs", StaticFiles(directory="docs"), name="docs")


def main():
    """Main entrypoint: uvicorn runs ASGI application directly."""
    import asyncio
    import uvicorn
    import config

    try:
        asyncio.run(config.init_config())
        host = os.environ.get("HOST") or asyncio.run(config.get_server_host())
        port = int(os.environ.get("PORT") or asyncio.run(config.get_server_port()))
    except Exception:
        port = int(os.environ.get("PORT", 7861))
        host = os.environ.get("HOST", "0.0.0.0")

    workers = int(os.environ.get("WORKERS", 1))

    log.info("=" * 60)
    log.info("Starting AGY2API")
    log.info("=" * 60)
    log.info(f"Host: {host}")
    log.info(f"Port: {port}")
    log.info(f"Control Panel: http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    if workers > 1:
        log.info(f"Worker count: {workers}")
    log.info("=" * 60)

    uvicorn.run(
        "web:app",
        host=host,
        port=port,
        workers=workers if workers > 1 else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
