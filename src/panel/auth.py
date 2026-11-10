"""
Authentication route module - Handles HTTP requests related to /auth/*
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from log import log
from src.auth import (
    asyncio_complete_auth_flow,
    complete_auth_flow_from_callback_url,
    create_auth_url,
    get_auth_status,
    verify_password,
)
from src.models import (
    LoginRequest,
    AuthStartRequest,
    AuthCallbackRequest,
    AuthCallbackUrlRequest,
)
from src.utils import verify_panel_token


# Create router
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(request: LoginRequest):
    """User login (simplified version: directly returns password as token)."""
    try:
        if await verify_password(request.password):
            # Directly use password as token to simplify authentication flow
            return JSONResponse(content={"token": request.password, "message": "Login successful"})
        else:
            raise HTTPException(status_code=401, detail="Incorrect password")
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Login failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/start")
async def start_auth(request: AuthStartRequest, token: str = Depends(verify_panel_token)):
    """Start authentication flow, supporting automatic project ID detection."""
    try:
        # If no project ID is provided, attempt automatic detection
        project_id = request.project_id
        if not project_id:
            log.info("User did not provide project ID; automatic detection will be used subsequently...")

        # Use auth token as user session identifier
        user_session = token if token else None
        result = await create_auth_url(
            project_id, user_session
        )

        if result["success"]:
            return JSONResponse(
                content={
                    "auth_url": result["auth_url"],
                    "state": result["state"],
                    "auto_project_detection": result.get("auto_project_detection", False),
                    "detected_project_id": result.get("detected_project_id"),
                }
            )
        else:
            raise HTTPException(status_code=500, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to start authentication flow: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/callback")
async def auth_callback(request: AuthCallbackRequest, token: str = Depends(verify_panel_token)):
    """Handle authentication callback, supporting automatic project ID detection."""
    try:
        # Project ID is now optional, auto-detected during callback processing
        project_id = request.project_id

        # Use auth token as user session identifier
        user_session = token if token else None
        # Asynchronously wait for OAuth callback completion
        result = await asyncio_complete_auth_flow(
            project_id, user_session
        )

        if result["success"]:
            # Single project authentication successful
            return JSONResponse(
                content={
                    "credentials": result["credentials"],
                    "email": result.get("email", ""),
                    "tier": result.get("tier"),
                    "message": "Authentication successful, credentials saved",
                    "auto_detected_project": result.get("auto_detected_project", False),
                }
            )
        else:
            # If manual project ID or project selection is required, indicate in response
            if result.get("requires_manual_project_id"):
                # Use JSON response
                return JSONResponse(
                    status_code=400,
                    content={"error": result["error"], "requires_manual_project_id": True},
                )
            elif result.get("requires_project_selection"):
                # Return project list for user selection
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": result["error"],
                        "requires_project_selection": True,
                        "available_projects": result["available_projects"],
                    },
                )
            else:
                raise HTTPException(status_code=400, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to process authentication callback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/callback-url")
async def auth_callback_url(request: AuthCallbackUrlRequest, token: str = Depends(verify_panel_token)):
    """Complete authentication directly from callback URL."""
    try:
        # Validate URL format
        if not request.callback_url or not request.callback_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Please provide a valid callback URL")

        # Complete authentication from callback URL
        result = await complete_auth_flow_from_callback_url(
            request.callback_url, request.project_id
        )

        if result["success"]:
            # Single project authentication successful
            return JSONResponse(
                content={
                    "credentials": result["credentials"],
                    "email": result.get("email", ""),
                    "tier": result.get("tier"),
                    "message": "Authentication successful from callback URL, credentials saved",
                    "auto_detected_project": result.get("auto_detected_project", False),
                }
            )
        else:
            # Handle various error conditions
            if result.get("requires_manual_project_id"):
                return JSONResponse(
                    status_code=400,
                    content={"error": result["error"], "requires_manual_project_id": True},
                )
            elif result.get("requires_project_selection"):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": result["error"],
                        "requires_project_selection": True,
                        "available_projects": result["available_projects"],
                    },
                )
            else:
                raise HTTPException(status_code=400, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to process authentication from callback URL: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{project_id}")
async def check_auth_status(project_id: str, token: str = Depends(verify_panel_token)):
    """Check authentication status."""
    try:
        if not project_id:
            raise HTTPException(status_code=400, detail="Project ID cannot be empty")

        status = get_auth_status(project_id)
        return JSONResponse(content=status)

    except Exception as e:
        log.error(f"Failed to check authentication status: {e}")
        raise HTTPException(status_code=500, detail=str(e))
