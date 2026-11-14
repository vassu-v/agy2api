"""
API Key Management Routes - Handles control panel endpoints for /keys/*
Protected by verify_panel_token (admin password authentication).
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from log import log
from src.api_key_manager import (
    generate_secure_key,
    hash_key,
    mask_key,
    reload_api_keys,
)
from src.storage.database import get_database
from src.utils import verify_panel_token
from src.router.antigravity.model_list import get_antigravity_models_with_features


router = APIRouter(prefix="/keys", tags=["api_keys"])


# ====================== Pydantic Request Models ======================

class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Friendly label for the API key")
    description: Optional[str] = Field(default="", max_length=1000)
    key: Optional[str] = Field(default=None, min_length=3, max_length=128, description="Custom auth password or auto-generated key")
    rpm_limit: Optional[int] = Field(default=0, ge=0, le=100000, description="Max requests per minute (0 = unlimited)")
    daily_limit: Optional[int] = Field(default=0, ge=0, le=10000000, description="Max requests per day (0 = unlimited)")
    token_limit: Optional[int] = Field(default=0, ge=0, description="Max lifetime token quota (0 = unlimited)")
    expires_at: Optional[float] = Field(default=None, description="Unix timestamp for key expiration (None = never)")
    models_allowed: Optional[List[str]] = Field(default_factory=list, description="Whitelisted models or [] for all")
    is_active: Optional[bool] = Field(default=True)


class UpdateApiKeyRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    key: Optional[str] = Field(default=None, min_length=3, max_length=128, description="Change auth password")
    description: Optional[str] = Field(default=None, max_length=1000)
    rpm_limit: Optional[int] = Field(default=None, ge=0, le=100000)
    daily_limit: Optional[int] = Field(default=None, ge=0, le=10000000)
    token_limit: Optional[int] = Field(default=None, ge=0)
    expires_at: Optional[float] = None
    models_allowed: Optional[List[str]] = None
    is_active: Optional[bool] = None


# ====================== API Endpoints ======================

@router.get("", response_model=List[Dict[str, Any]])
async def list_keys(token: str = Depends(verify_panel_token)):
    """List all configured API keys. Raw secret keys are masked for security."""
    try:
        db = await get_database()
        keys_list = await db.list_api_keys()
        for k in keys_list:
            raw_key = k.get("key", "")
            k["masked_key"] = mask_key(raw_key)
        return keys_list
    except Exception as e:
        log.error(f"Failed to list API keys: {e}")
        raise HTTPException(status_code=500, detail="Failed to list API keys")


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_key(req: CreateApiKeyRequest, token: str = Depends(verify_panel_token)):
    """Create a new API key. Full key string is returned once on creation."""
    raw_key = req.key.strip() if req.key and req.key.strip() else generate_secure_key()
    kh = hash_key(raw_key)

    db = await get_database()
    # Check duplicate
    existing = await db.get_api_key_by_hash(kh)
    if existing:
        raise HTTPException(status_code=400, detail="An API key with this value already exists")

    key_data = {
        "key": raw_key,
        "name": req.name.strip(),
        "description": (req.description or "").strip(),
        "rpm_limit": req.rpm_limit or 0,
        "daily_limit": req.daily_limit or 0,
        "token_limit": req.token_limit or 0,
        "expires_at": req.expires_at,
        "models_allowed": req.models_allowed or [],
        "is_active": req.is_active if req.is_active is not None else True,
    }

    try:
        created = await db.create_api_key(key_data)
        await reload_api_keys()
        created["masked_key"] = mask_key(raw_key)
        # Full key returned for initial user copy
        created["key"] = raw_key
        log.info(f"Created new API key: '{created['name']}' (ID: {created['id']})")
        return created
    except IntegrityError:
        raise HTTPException(status_code=400, detail="An API key with this value or hash already exists")
    except Exception as e:
        log.error(f"Failed to create API key: {e}")
        raise HTTPException(status_code=500, detail="Failed to create API key")


@router.put("/{key_id}", response_model=Dict[str, Any])
async def update_key(key_id: int, req: UpdateApiKeyRequest, token: str = Depends(verify_panel_token)):
    """Update settings, status, limits, or whitelisted models for an API key."""
    db = await get_database()
    existing = await db.get_api_key_by_id(key_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")

    # Use model_dump(exclude_unset=True) to allow explicitly setting expires_at to None
    provided_fields = req.model_dump(exclude_unset=True)
    updates: Dict[str, Any] = {}

    if "name" in provided_fields and provided_fields["name"]:
        updates["name"] = provided_fields["name"].strip()
    if "key" in provided_fields and provided_fields["key"]:
        new_raw_key = provided_fields["key"].strip()
        new_kh = hash_key(new_raw_key)
        existing_other = await db.get_api_key_by_hash(new_kh)
        if existing_other and existing_other["id"] != key_id:
            raise HTTPException(status_code=400, detail="This auth password is already in use by another key")
        updates["key"] = new_raw_key
    if "description" in provided_fields:
        updates["description"] = (provided_fields["description"] or "").strip()
    if "rpm_limit" in provided_fields:
        updates["rpm_limit"] = provided_fields["rpm_limit"]
    if "daily_limit" in provided_fields:
        updates["daily_limit"] = provided_fields["daily_limit"]
    if "token_limit" in provided_fields:
        updates["token_limit"] = provided_fields["token_limit"]
    if "expires_at" in provided_fields:
        updates["expires_at"] = provided_fields["expires_at"]  # can be None to remove expiration
    if "models_allowed" in provided_fields:
        updates["models_allowed"] = provided_fields["models_allowed"]
    if "is_active" in provided_fields:
        updates["is_active"] = provided_fields["is_active"]

    try:
        updated = await db.update_api_key(key_id, updates)
        await reload_api_keys()

        if not updated:
            raise HTTPException(status_code=404, detail="API key not found")

        updated["masked_key"] = mask_key(updated.get("key", ""))
        log.info(f"Updated API key ID {key_id} ('{updated.get('name', '')}')")
        return updated
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to update API key {key_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update API key")


@router.delete("/{key_id}", response_model=Dict[str, Any])
async def delete_key(key_id: int, token: str = Depends(verify_panel_token)):
    """Delete an API key."""
    try:
        db = await get_database()
        existing = await db.get_api_key_by_id(key_id)
        if not existing:
            raise HTTPException(status_code=404, detail="API key not found")

        success = await db.delete_api_key(key_id)
        await reload_api_keys()
        log.info(f"Deleted API key ID {key_id} ('{existing.get('name')}')")
        return {"success": success, "message": "API key deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to delete API key {key_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete API key")


@router.get("/generate/candidate", response_model=Dict[str, str])
async def generate_candidate_key(token: str = Depends(verify_panel_token)):
    """Generate a random candidate key token (for the modal auto-fill)."""
    return {"key": generate_secure_key()}


@router.get("/available-models", response_model=List[str])
async def get_available_models(token: str = Depends(verify_panel_token)):
    """Fetch live available model names (including base models and aliases) for restriction selection."""
    try:
        models = await get_antigravity_models_with_features()
        base_models = [m for m in models if not m.startswith("autostitch-") and not m.startswith("抗截断-")]
        variants = [m for m in models if m not in base_models]
        return base_models + variants
    except Exception as e:
        log.warning(f"Failed to fetch available models for panel: {e}")
        return [
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-tiered",
            "claude-sonnet-4-6",
            "claude-sonnet-4-6-thinking",
            "claude-opus-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ]
