from typing import Optional

import httpx

from config import get_api_password, get_panel_password
from fastapi import Depends, HTTPException, Header, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from log import log

# HTTP Bearer security scheme
security = HTTPBearer()

# ====================== OAuth Configuration ======================

# Antigravity CLI client emulation constants
_DEFAULT_ANTIGRAVITY_CLI_VERSION = "1.5.3"
_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/google-antigravity/antigravity-cli/releases/latest"
)


def _get_latest_antigravity_cli_version() -> str:
    """Fetch latest Antigravity CLI version; returns default version on failure."""
    try:
        response = httpx.get(
            _LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        response.raise_for_status()
        release_data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        log.warning(
            "Failed to fetch latest Antigravity CLI version, using default version "
            f"{_DEFAULT_ANTIGRAVITY_CLI_VERSION}: {error}"
        )
        return _DEFAULT_ANTIGRAVITY_CLI_VERSION

    tag_name = release_data.get("tag_name") if isinstance(release_data, dict) else None
    if not isinstance(tag_name, str) or not tag_name.strip():
        log.warning(
            "GitHub API response lacks valid tag_name, using default version "
            f"{_DEFAULT_ANTIGRAVITY_CLI_VERSION}"
        )
        return _DEFAULT_ANTIGRAVITY_CLI_VERSION

    return tag_name.strip().lstrip("vV")


ANTIGRAVITY_CLI_VERSION = _get_latest_antigravity_cli_version()
ANTIGRAVITY_CLI_PLATFORM = "windows/amd64"
ANTIGRAVITY_USER_AGENT = f"antigravity/cli/{ANTIGRAVITY_CLI_VERSION} {ANTIGRAVITY_CLI_PLATFORM}"
log.info(f"Current Antigravity CLI version: {ANTIGRAVITY_CLI_VERSION}")

# Antigravity OAuth Configuration
ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"
ANTIGRAVITY_SCOPES = [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/cclog',
    'https://www.googleapis.com/auth/experimentsandconfigs'
]

# Unified Token URL (same for both modes)
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Callback server configuration
CALLBACK_HOST = "localhost"

# ====================== Model Helper Functions ======================

# Autostitch / Anti-truncation model prefixes (keeps legacy Chinese alias for backward compatibility)
_ANTI_TRUNCATION_PREFIXES = ("autostitch-", "抗截断-")


def is_anti_truncation_model(model_name: str) -> bool:
    """Check if model name indicates autostitch / anti-truncation should be used."""
    return any(model_name.startswith(p) for p in _ANTI_TRUNCATION_PREFIXES)


def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    for p in _ANTI_TRUNCATION_PREFIXES:
        if model_name.startswith(p):
            return model_name[len(p) :]
    return model_name


# ====================== Authentication Functions ======================

async def authenticate_flexible(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    access_token: Optional[str] = Header(None, alias="access_token"),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    x_anthropic_auth_token: Optional[str] = Header(None, alias="x-anthropic-auth-token"),
    anthropic_auth_token: Optional[str] = Header(None, alias="anthropic-auth-token"),
    key: Optional[str] = Query(None)
) -> str:
    """
    Unified flexible authentication function supporting multiple authentication mechanisms.

    This function can be directly used as a FastAPI Depends dependency.

    Supported authentication methods:
        - URL parameter: key
        - HTTP header: Authorization (Bearer token)
        - HTTP header: x-api-key
        - HTTP header: access_token
        - HTTP header: x-goog-api-key
        - HTTP header: x-anthropic-auth-token
        - HTTP header: anthropic-auth-token

    Args:
        request: FastAPI Request object
        authorization: Authorization header value (auto injected)
        x_api_key: x-api-key header value (auto injected)
        access_token: access_token header value (auto injected)
        x_goog_api_key: x-goog-api-key header value (auto injected)
        x_anthropic_auth_token: x-anthropic-auth-token header value (auto injected)
        anthropic_auth_token: anthropic-auth-token header value (auto injected)
        key: URL parameter key (auto injected)

    Returns:
        Validated token string

    Raises:
        HTTPException: Raised when authentication fails

    Usage:
        @router.post("/endpoint")
        async def endpoint(token: str = Depends(authenticate_flexible)):
            # token validated
            pass
    """
    password = await get_api_password()
    token = None
    auth_method = None

    # 1. Try URL parameter key (Google official standard)
    if key:
        token = key
        auth_method = "URL parameter 'key'"

    # 2. Try x-goog-api-key header (Google API standard)
    elif x_goog_api_key:
        token = x_goog_api_key
        auth_method = "x-goog-api-key header"

    # 3. Try x-anthropic-auth-token header (Anthropic standard)
    elif x_anthropic_auth_token:
        token = x_anthropic_auth_token
        auth_method = "x-anthropic-auth-token header"

    # 4. Try anthropic-auth-token header (Anthropic alternative)
    elif anthropic_auth_token:
        token = anthropic_auth_token
        auth_method = "anthropic-auth-token header"

    # 5. Try x-api-key header
    elif x_api_key:
        token = x_api_key
        auth_method = "x-api-key header"

    # 6. Try access_token header
    elif access_token:
        token = access_token
        auth_method = "access_token header"

    # 7. Try Authorization header
    elif authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization[7:]  # Remove "Bearer " prefix
        auth_method = "Authorization Bearer header"

    # Check whether any authentication credential was provided
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials. Use 'key' URL parameter, 'x-goog-api-key', 'x-anthropic-auth-token', 'anthropic-auth-token', 'x-api-key', 'access_token' header, or 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Verify token
    if token != password:
        log.debug(f"Authentication failed using {auth_method}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incorrect password"
        )
    
    log.debug(f"Authentication successful using {auth_method}")
    return token


# Backward compatibility aliases
authenticate_bearer = authenticate_flexible
authenticate_gemini_flexible = authenticate_flexible


# ====================== Panel Authentication Functions ======================

async def verify_panel_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Verify panel bearer token against panel password."""
    password = await get_panel_password()
    if credentials.credentials != password:
        raise HTTPException(status_code=401, detail="Incorrect password")
    return credentials.credentials
