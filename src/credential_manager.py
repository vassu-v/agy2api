"""
Credential Management (stateless functional wrapper).
Manages Antigravity credentials keyed by email: load-balancing selection, automatic token refresh, error recording, and model-level cooldown.

All functions interact directly with the global database singleton (get_database) without instance state;
the sole in-process state is the current sticky credential pointer for sticky mode.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from log import log

from config import get_credential_select_mode
from src import session_cache
from src.google_oauth_api import Credentials
from src.storage.database import get_database

# Current sticky credential pointer for sticky mode (sole in-process state; reset function for testing)
_sticky_email: Optional[str] = None


def _reset_sticky() -> None:
    """Clear sticky credential pointer (testing)."""
    global _sticky_email
    _sticky_email = None


# ====================== Credential Selection ======================

async def get_valid_credential(
    model_name: Optional[str] = None,
    session_key: Optional[str] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Get available credential: picks by configured selection mode, automatically skipping disabled/cooldown credentials.
    Automatically refreshes token on expiry; disables credential and tries next if refresh fails permanently.

    Credential selection modes (config: credential_select_mode):
    - random: Pure random load balancing (ignores session_key)
    - cache:  Session cache match - reuses bound credential when session_key hits (prompt cache affinity)
    - sticky: Sticky - keeps same credential until cooled down or disabled

    Returns:
        (email, credential_data) or None
    """
    global _sticky_email
    database = await get_database()
    mode = await get_credential_select_mode()

    # cache mode: check session binding first
    if mode == "cache" and session_key:
        bound_email = await session_cache.get_session_binding(session_key)
        if bound_email:
            result = await database.get_available_credential_by_email(
                bound_email, model_name=model_name
            )
            if result:
                log.debug(f"Session cache hit: {bound_email}")
                final = await _validate_or_refresh(result[0], result[1])
                if final is None:
                    # Bound credential permanently failed; unbind and proceed to normal selection
                    await session_cache.delete_session_binding(session_key)
                else:
                    return final
            else:
                # Bound credential unavailable (disabled/CD/deleted); unbind
                await session_cache.delete_session_binding(session_key)

    # sticky mode: prioritize current sticky credential
    if mode == "sticky" and _sticky_email:
        result = await database.get_available_credential_by_email(
            _sticky_email, model_name=model_name
        )
        if result:
            final = await _validate_or_refresh(result[0], result[1])
            if final is not None:
                return final
        # Sticky credential entered CD/disabled/failed; clear pointer to pick new
        _sticky_email = None

    # General selection (random mode / cache miss / sticky without available credential)
    max_retries = 3
    for attempt in range(max_retries):
        result = await database.get_next_available_credential(model_name=model_name)

        if not result:
            if attempt == 0:
                log.warning(f"No available credentials (model_name={model_name})")
            return None

        email, credential_data = result

        if await should_refresh_token(credential_data):
            log.debug(f"Token refresh required: {email}")
            refreshed = await refresh_token(credential_data, email)
            if refreshed:
                await _after_picked(mode, email, session_key)
                return email, refreshed
            log.warning(
                f"Token refresh failed, trying next credential: {email} (attempt={attempt + 1}/{max_retries})"
            )
            continue

        await _after_picked(mode, email, session_key)
        return email, credential_data

    log.error(f"No available credentials after {max_retries} attempts (model_name={model_name})")
    return None


async def _validate_or_refresh(
    email: str, credential_data: Dict[str, Any]
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Validate token of selected credential; refresh if needed, return None if permanently failed."""
    if await should_refresh_token(credential_data):
        refreshed = await refresh_token(credential_data, email)
        if refreshed is None:
            return None
        credential_data = refreshed
    return email, credential_data


async def _after_picked(mode: str, email: str, session_key: Optional[str]) -> None:
    """Post-selection handling per mode."""
    global _sticky_email
    try:
        if mode == "cache" and session_key:
            await session_cache.set_session_binding(session_key, email)
        elif mode == "sticky":
            _sticky_email = email
    except Exception as e:
        log.debug(f"Failed to record credential affinity (non-fatal): {e}")


# ====================== Credential CRUD ======================

async def add_credential(email: str, credential_data: Dict[str, Any]):
    """Add or update credential (overwrites same email, deduplicating automatically)."""
    database = await get_database()
    await database.store_credential(email, credential_data)
    log.info(f"Credential added/updated: {email}")


async def remove_credential(email: str) -> bool:
    """Remove credential."""
    database = await get_database()
    try:
        await database.delete_credential(email)
        log.info(f"Credential removed: {email}")
        return True
    except Exception as e:
        log.error(f"Error removing credential {email}: {e}")
        return False


async def update_credential_state(email: str, state_updates: Dict[str, Any]) -> bool:
    """Update credential state fields (disabled/error_codes/tier/project_id etc.)."""
    database = await get_database()
    try:
        success = await database.update_credential_state(email, state_updates)
        if not success:
            log.warning(f"Failed to update credential state: {email}")
        return success
    except Exception as e:
        log.error(f"Error updating credential state {email}: {e}")
        return False


async def set_cred_disabled(email: str, disabled: bool):
    """Set credential enabled/disabled status."""
    success = await update_credential_state(email, {"disabled": disabled})
    if success:
        log.info(f"Credential {'disabled' if disabled else 'enabled'}: {email}")
    return success


# ====================== API Call Result Recording ======================

async def record_api_call_result(
    email: str,
    success: bool,
    error_code: Optional[int] = None,
    cooldown_until: Optional[float] = None,
    model_name: Optional[str] = None,
    error_message: Optional[str] = None,
):
    """
    Record API call outcome:
    - success: Clears error state and model cooldown (fire-and-forget)
    - failure: Records error code/message, applies model-level cooldown if needed
    """
    database = await get_database()
    try:
        if success:
            asyncio.create_task(database.record_success(email, model_name=model_name))
        elif error_code:
            error_messages = {}
            if error_message:
                error_messages[str(error_code)] = error_message

            await update_credential_state(email, {
                "error_codes": [error_code],
                "error_messages": error_messages,
            })

            if cooldown_until is not None and model_name:
                await database.set_model_cooldown(email, model_name, cooldown_until)
                log.info(
                    f"Set model cooldown: {email}, model={model_name}, "
                    f"until: {datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()}"
                )
    except Exception as e:
        log.error(f"Error recording API call result for {email}: {e}")


# ====================== Token Refresh ======================

async def should_refresh_token(credential_data: Dict[str, Any]) -> bool:
    """Check if token needs refresh (less than 5 minutes remaining considered expiring)."""
    access_token = credential_data.get("access_token") or credential_data.get("token")
    if not access_token:
        return True

    expiry_str = credential_data.get("expiry")
    if not expiry_str:
        return True

    try:
        if not isinstance(expiry_str, str):
            return True
        if "+" in expiry_str:
            expiry = datetime.fromisoformat(expiry_str)
        elif expiry_str.endswith("Z"):
            expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        else:
            expiry = datetime.fromisoformat(expiry_str)

        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        time_left = (expiry - datetime.now(timezone.utc)).total_seconds()
        return time_left <= 300  # 5-minute buffer

    except Exception as e:
        log.warning(f"Failed to parse expiry timestamp: {e}, refresh needed")
        return True


async def refresh_token(
    credential_data: Dict[str, Any], email: str
) -> Optional[Dict[str, Any]]:
    """Refresh token and persist; auto-disables credential on permanent failure, returning None."""
    database = await get_database()
    try:
        creds = Credentials.from_dict(credential_data)

        if not creds.refresh_token:
            log.error(f"No refresh_token found, cannot refresh: {email}")
            await update_credential_state(email, {"disabled": True})
            log.warning(f"Credential auto-disabled (missing refresh_token): {email}")
            return None

        log.debug(f"Refreshing token: {email}")
        await creds.refresh()

        if creds.access_token:
            credential_data["access_token"] = creds.access_token
            credential_data["token"] = creds.access_token
        if creds.expires_at:
            credential_data["expiry"] = creds.expires_at.isoformat() if hasattr(creds.expires_at, "isoformat") else str(creds.expires_at)

        await database.store_credential(email, credential_data)
        log.info(f"Token refreshed and stored successfully: {email}")
        return credential_data

    except Exception as e:
        error_msg = str(e)
        log.error(f"Token refresh failed for {email}: {error_msg}")

        status_code = getattr(e, "status_code", None)

        if _is_permanent_refresh_failure(error_msg, status_code):
            log.warning(f"Permanent credential failure detected (HTTP {status_code}): {email}")
            await record_api_call_result(email, False, error_code=status_code or 400)
            if await update_credential_state(email, {"disabled": True}):
                log.warning(f"Permanently failed credential disabled: {email}")
        else:
            log.warning(f"Token refresh failed with non-permanent error (HTTP {status_code}), credential not banned: {email}")

        return None


def _is_permanent_refresh_failure(error_msg: str, status_code: Optional[int] = None) -> bool:
    """Determine whether token refresh failure is permanent (client error / explicit revocation)."""
    if status_code is not None:
        if status_code in (400, 401, 403):
            return True
        if status_code in (500, 502, 503, 504):
            return False
        if status_code == 429:
            return False

    permanent_patterns = (
        "invalid_grant",
        "refresh_token_expired",
        "invalid_refresh_token",
        "unauthorized_client",
        "access_denied",
    )
    error_msg_lower = error_msg.lower()
    return any(p in error_msg_lower for p in permanent_patterns)
