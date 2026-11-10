"""
Root route module - Serves control panel home page (compiled frontend)
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from log import log


# Create router
router = APIRouter(tags=["root"])

# Compiled frontend directory
FRONTEND_DIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "dist",
)


def _safe_join_dist(path: str) -> str:
    """Safely join frontend static asset path to prevent path traversal."""
    full = os.path.normpath(os.path.join(FRONTEND_DIST_DIR, path))
    dist_root = os.path.normpath(FRONTEND_DIST_DIR)
    if not full.startswith(dist_root + os.sep) and full != dist_root:
        raise HTTPException(status_code=404, detail="Not Found")
    return full


@router.get("/", response_class=HTMLResponse)
async def serve_control_panel():
    """Serve compiled control panel home page."""
    index_path = os.path.join(FRONTEND_DIST_DIR, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="Frontend assets not built. Please run 'npm run build' in the frontend directory first.",
        )
    except Exception as e:
        log.error(f"Failed to load control panel page: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/assets/{file_path:path}")
async def serve_frontend_assets(file_path: str):
    """Serve frontend static assets (JS/CSS, etc.)."""
    full_path = _safe_join_dist(os.path.join("assets", file_path))
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(full_path)


@router.get("/favicon.ico")
async def serve_favicon():
    """Serve favicon."""
    # Prefer serving from compiled frontend directory
    favicon_path = os.path.join(FRONTEND_DIST_DIR, "favicon.ico")
    if os.path.isfile(favicon_path):
        return FileResponse(favicon_path, media_type="image/x-icon")
    raise HTTPException(status_code=404, detail="Not Found")
