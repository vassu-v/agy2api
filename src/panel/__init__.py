"""
Panel module - Integrates all control panel routes
"""

from fastapi import APIRouter

from . import auth, creds, config_routes, version, root, usage_logs, api_keys


def create_router() -> APIRouter:
    """Create and return the main router integrating all sub-routers."""
    router = APIRouter()

    # Include all sub-routers
    router.include_router(root.router)
    router.include_router(auth.router)
    router.include_router(creds.router)
    router.include_router(config_routes.router)
    router.include_router(version.router)
    router.include_router(usage_logs.router)
    router.include_router(api_keys.router)

    return router


# Export main router
router = create_router()

__all__ = ["router"]
