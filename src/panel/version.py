"""
Version information route module - Handles HTTP requests related to /version/*
"""

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from log import log


# Create router
router = APIRouter(prefix="/version", tags=["version"])


@router.get("/info")
async def get_version_info(check_update: bool = False):
    """
    Get current version information - Reads from version.txt (local only).
    The check_update parameter is accepted for frontend compatibility but ignored:
    no remote update check is performed.
    """
    try:
        # Get project root directory
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        version_file = os.path.join(project_root, "version.txt")

        # Read version.txt
        if not os.path.exists(version_file):
            return JSONResponse({
                "success": False,
                "error": "version.txt file does not exist"
            })

        version_data = {}
        with open(version_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if '=' in line:
                    key, value = line.split('=', 1)
                    version_data[key] = value

        # Check required fields
        if 'short_hash' not in version_data:
            return JSONResponse({
                "success": False,
                "error": "version.txt format error"
            })

        response_data = {
            "success": True,
            "version": version_data.get('short_hash', 'unknown'),
            "full_hash": version_data.get('full_hash', ''),
            "message": version_data.get('message', ''),
            "date": version_data.get('date', '')
        }

        # No remote update check: version info is read from the local version.txt only
        response_data['check_update'] = False

        return JSONResponse(response_data)

    except Exception as e:
        log.error(f"Failed to get version info: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        })
