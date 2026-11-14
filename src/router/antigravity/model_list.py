"""
Antigravity Model List Router - Handles model list requests
"""

import sys
from pathlib import Path

# Add project root directory to Python path
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Third-party libraries
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# Local modules - Utilities and authentication
from src.utils import (
    get_base_model_from_feature_model,
    authenticate_flexible
)

# Local modules - API
from src.api.antigravity import fetch_available_models

# Local modules - Base router utilities
from src.router.base_router import create_gemini_model_list, create_openai_model_list
from src.models import model_to_dict
from log import log


# ==================== Router Initialization ====================

router = APIRouter()


# ==================== Helper Functions ====================

async def get_antigravity_models_with_features():
    """
    Get Antigravity model list, add feature prefixes and serving aliases,
    and filter by visible_models configuration if specified.
    
    Returns:
        List of models available for serving
    """
    import config

    # Fetch base model list from API
    base_models_data = await fetch_available_models()
    
    if not base_models_data:
        log.warning("[ANTIGRAVITY MODEL LIST] Unable to fetch model list, returning empty list")
        base_model_ids = []
    else:
        base_model_ids = [model['id'] for model in base_models_data if 'id' in model]
    
    # Add feature prefixes
    models = []
    for base_model in base_model_ids:
        # Base model
        models.append(base_model)
        
        # Autostitch model (English prefix format)
        models.append(f"autostitch-{base_model}")

    # Include custom alias model names from rewrite rules (if simple names without regex tokens)
    try:
        rewrite_rules = await config.get_model_rewrite_rules_config()
        for rule in rewrite_rules:
            if isinstance(rule, (list, tuple)) and len(rule) == 2:
                pat = str(rule[0]).strip().lstrip("^").rstrip("$")
                if pat and not any(c in pat for c in r"*+?[](){}|\."):
                    if pat not in models:
                        models.append(pat)
    except Exception as e:
        log.debug(f"[ANTIGRAVITY MODEL LIST] Error parsing alias models from rewrite rules: {e}")

    # Filter by visible_models if configured
    try:
        visible_models = await config.get_visible_models()
        if visible_models:
            filtered = []
            for m in models:
                if m in visible_models or any(vm in m for vm in visible_models):
                    filtered.append(m)
            # Ensure any model explicitly listed in visible_models is included
            for vm in visible_models:
                if vm not in filtered:
                    filtered.append(vm)
            log.info(f"[ANTIGRAVITY MODEL LIST] Filtered to {len(filtered)} visible models")
            return filtered
    except Exception as e:
        log.warning(f"[ANTIGRAVITY MODEL LIST] Error filtering visible models: {e}")
    
    log.info(f"[ANTIGRAVITY MODEL LIST] Generated {len(models)} models (including feature prefixes and aliases)")
    return models


# ==================== API Routes ====================

def _filter_models_for_request(request: Request, models: list) -> list:
    """Filter model list by API key permissions if restricted."""
    if not hasattr(request, "state"):
        return models
    api_key = getattr(request.state, "api_key", None)
    if not api_key or api_key.get("is_admin", False):
        return models
    allowed = api_key.get("models_allowed", [])
    if not allowed:
        return models
    allowed_norm = {m.lower().strip() for m in allowed if m.strip()}
    return [
        m for m in models
        if m.lower() in allowed_norm or get_base_model_from_feature_model(m).lower() in allowed_norm
    ]


@router.get("/v1beta/models")
async def list_gemini_models(request: Request, token: str = Depends(authenticate_flexible)):
    """
    Return model list in Gemini format
    
    Dynamically fetches model list from src.api.antigravity.fetch_available_models
    and adds anti-truncation prefixes
    """
    models = await get_antigravity_models_with_features()
    models = _filter_models_for_request(request, models)
    log.info("[ANTIGRAVITY MODEL LIST] Returning Gemini format")
    return JSONResponse(content=create_gemini_model_list(
        models,
        base_name_extractor=get_base_model_from_feature_model
    ))


@router.get("/v1/models")
async def list_openai_models(request: Request, token: str = Depends(authenticate_flexible)):
    """
    Return model list in OpenAI format
    
    Dynamically fetches model list from src.api.antigravity.fetch_available_models
    and adds anti-truncation prefixes
    """
    models = await get_antigravity_models_with_features()
    models = _filter_models_for_request(request, models)
    log.info("[ANTIGRAVITY MODEL LIST] Returning OpenAI format")
    model_list = create_openai_model_list(models, owned_by="google")
    return JSONResponse(content={
        "object": "list",
        "data": [model_to_dict(model) for model in model_list.data]
    })
