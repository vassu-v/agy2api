"""
Credential management route module - Handles HTTP requests related to /creds/* (Antigravity mode only)
Credentials use email as the unique key; import/export uses standard JSON format:
  { "refresh_token": "...", "access_token": "...", "expiry": "...", "email": "...", "tier": "...", "project_id": "..." }
"""

import asyncio
import io
import json
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Response
from fastapi.responses import JSONResponse

from log import log
from src.credential_manager import (
    add_credential,
    remove_credential,
    set_cred_disabled,
)
from src.models import (
    CredFileActionRequest,
    CredFileBatchActionRequest
)
from src.storage.database import get_database
from src.utils import (
    verify_panel_token,
    ANTIGRAVITY_USER_AGENT,
    ANTIGRAVITY_CLIENT_ID,
    ANTIGRAVITY_CLIENT_SECRET,
)
from src.api.antigravity import fetch_quota_info, build_antigravity_headers
from src.httpx_client import post_async
from src.google_oauth_api import (
    Credentials,
    fetch_project_id_and_tier,
    get_user_email,
)
from config import get_antigravity_api_url


# Create router
router = APIRouter(prefix="/creds", tags=["credentials"])


# =============================================================================
# Credential Import Core
# =============================================================================

async def _import_single_credential(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Import a single credential (standard format).
    Required field: refresh_token
    Optional fields: email / tier / project_id / access_token / expiry
    (If access_token is not expired, reuse it directly; otherwise refresh using refresh_token, and automatically retrieve other missing fields)

    Returns:
        {"status": "success"|"error", "email": ..., "message": ...}
    """
    try:
        refresh_token = (data.get("refresh_token") or "").strip()
        if not refresh_token:
            return {"status": "error", "message": "Missing refresh_token"}

        email = (data.get("email") or "").strip().lower() or None
        tier = (data.get("tier") or "").strip().lower() or None
        project_id = (data.get("project_id") or "").strip() or None
        access_token = (str(data.get("access_token") or "")).strip()
        expiry = (str(data.get("expiry") or "")).strip()

        # If unexpired access_token is provided, reuse it directly (skip refresh to avoid rate limiting during batch imports)
        creds = None
        if access_token:
            candidate = Credentials.from_dict({
                "access_token": access_token,
                "refresh_token": refresh_token,
                "client_id": ANTIGRAVITY_CLIENT_ID,
                "client_secret": ANTIGRAVITY_CLIENT_SECRET,
                "expiry": expiry,
            })
            if not candidate.is_expired():
                creds = candidate

        # If no usable access_token, exchange refresh_token for access_token while validating credential validity
        if creds is None:
            creds = Credentials(
                access_token="",
                refresh_token=refresh_token,
                client_id=ANTIGRAVITY_CLIENT_ID,
                client_secret=ANTIGRAVITY_CLIENT_SECRET,
            )
            try:
                await creds.refresh()
            except Exception as e:
                return {"status": "error", "message": f"Invalid refresh_token: {str(e)[:200]}"}

        if not creds.access_token:
            return {"status": "error", "message": "Invalid refresh_token: failed to obtain access_token"}

        # Automatically fetch missing email
        if not email:
            try:
                email = await get_user_email(creds)
            except Exception as e:
                log.warning(f"Failed to automatically get email: {e}")
        if not email:
            return {"status": "error", "message": "Unable to obtain email (not provided in JSON and auto-fetch failed)"}

        # Automatically fetch missing project_id / tier
        if not project_id or not tier:
            try:
                api_base_url = await get_antigravity_api_url()
                fetched_project, fetched_tier = await fetch_project_id_and_tier(
                    access_token=creds.access_token,
                    user_agent=ANTIGRAVITY_USER_AGENT,
                    api_base_url=api_base_url,
                )
                if not project_id:
                    project_id = fetched_project
                if not tier:
                    tier = fetched_tier
            except Exception as e:
                log.warning(f"Failed to automatically get project_id/tier: {e}")

        # If project_id fetch fails, fall back to aicode-consumers to avoid storing null values
        if not project_id:
            log.warning(f"Import credential {email}: project_id fetch failed, using fallback value aicode-consumers")
            project_id = "aicode-consumers"

        credential_data = {
            "access_token": creds.access_token,
            "refresh_token": refresh_token,
            "client_id": ANTIGRAVITY_CLIENT_ID,
            "client_secret": ANTIGRAVITY_CLIENT_SECRET,
            "project_id": project_id,
            "expiry": creds.expires_at.isoformat() if creds.expires_at else "",
            "tier": tier,
        }

        await add_credential(email, credential_data)
        return {
            "status": "success",
            "email": email,
            "tier": tier,
            "message": "Import successful",
        }

    except Exception as e:
        return {"status": "error", "message": f"Processing failed: {str(e)[:200]}"}


# =============================================================================
# Utility Functions
# =============================================================================


async def extract_json_files_from_zip(zip_file: UploadFile) -> List[dict]:
    """Extract JSON files from ZIP file."""
    try:
        zip_content = await zip_file.read()
        files_data = []

        with zipfile.ZipFile(io.BytesIO(zip_content), "r") as zip_ref:
            file_list = zip_ref.namelist()
            json_files = [
                f for f in file_list if f.endswith(".json") and not f.startswith("__MACOSX/")
            ]

            if not json_files:
                raise HTTPException(status_code=400, detail="No JSON files found in ZIP file")

            log.info(f"Found {len(json_files)} JSON files in ZIP file {zip_file.filename}")

            for json_filename in json_files:
                try:
                    with zip_ref.open(json_filename) as json_file:
                        content = json_file.read()

                        try:
                            content_str = content.decode("utf-8")
                        except UnicodeDecodeError:
                            log.warning(f"Skipping file with encoding error: {json_filename}")
                            continue

                        filename = json_filename.rsplit("/", 1)[-1]
                        files_data.append({"filename": filename, "content": content_str})

                except Exception as e:
                    log.warning(f"Error processing file {json_filename} in ZIP: {e}")
                    continue

        log.info(f"Successfully extracted {len(files_data)} valid JSON files from ZIP file")
        return files_data

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file format")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        log.error(f"Failed to process ZIP file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process ZIP file: {str(e)}")


async def upload_credentials_common( files: List[UploadFile] ) -> JSONResponse:
    """Batch import credential files (standard JSON: refresh_token required, access_token/expiry/email/tier/project_id optional)."""

    if not files:
        raise HTTPException(status_code=400, detail="Please select files to upload")

    if len(files) > 100:
        raise HTTPException(
            status_code=400, detail=f"Too many files, maximum 100 files supported, current: {len(files)}"
        )

    files_data = []
    for file in files:
        if file.filename.endswith(".zip"):
            zip_files_data = await extract_json_files_from_zip(file)
            files_data.extend(zip_files_data)
            log.info(f"Extracted {len(zip_files_data)} JSON files from ZIP file {file.filename}")

        elif file.filename.endswith(".json"):
            content_chunks = []
            while True:
                chunk = await file.read(8192)
                if not chunk:
                    break
                content_chunks.append(chunk)

            content = b"".join(content_chunks)
            try:
                content_str = content.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400, detail=f"File {file.filename} encoding format not supported"
                )

            files_data.append({"filename": file.filename, "content": content_str})
        else:
            raise HTTPException(
                status_code=400, detail=f"File {file.filename} format not supported, only JSON and ZIP files are supported"
            )

    batch_size = 100
    all_results = []
    total_success = 0

    for i in range(0, len(files_data), batch_size):
        batch_files = files_data[i : i + batch_size]

        async def process_single_file(file_data):
            try:
                data = json.loads(file_data["content"])
                if not isinstance(data, dict):
                    return {
                        "filename": file_data["filename"],
                        "status": "error",
                        "message": "JSON format error: top level must be an object",
                    }
                result = await _import_single_credential(data)
                result["filename"] = file_data["filename"]
                return result
            except json.JSONDecodeError as e:
                return {
                    "filename": file_data["filename"],
                    "status": "error",
                    "message": f"JSON format error: {str(e)}",
                }
            except Exception as e:
                return {
                    "filename": file_data["filename"],
                    "status": "error",
                    "message": f"Processing failed: {str(e)[:200]}",
                }

        log.info(f"Starting concurrent processing of {len(batch_files)} credentials...")
        concurrent_tasks = [process_single_file(file_data) for file_data in batch_files]
        batch_results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)

        batch_uploaded_count = 0
        for result in batch_results:
            if isinstance(result, Exception):
                all_results.append(
                    {
                        "filename": "unknown",
                        "status": "error",
                        "message": f"Processing exception: {str(result)[:200]}",
                    }
                )
            else:
                all_results.append(result)
                if result["status"] == "success":
                    batch_uploaded_count += 1

        total_success += batch_uploaded_count

        batch_num = (i // batch_size) + 1
        total_batches = (len(files_data) + batch_size - 1) // batch_size
        log.info(
            f"Batch {batch_num}/{total_batches} completed: successfully processed "
            f"{batch_uploaded_count}/{len(batch_files)} credentials"
        )

    return JSONResponse(
        content={
            "uploaded_count": total_success,
            "total_count": len(files_data),
            "results": all_results,
            "message": f"Batch import completed: successfully processed {total_success}/{len(files_data)} credentials",
        }
    )


async def get_creds_status_common(
    offset: int, limit: int, status_filter: str,
    error_code_filter: str = None, cooldown_filter: str = None, tier_filter: str = None
) -> JSONResponse:
    """Get credential status."""
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be greater than or equal to 0")
    if limit not in [20, 50, 100, 200, 500, 1000]:
        raise HTTPException(status_code=400, detail="limit can only be 20, 50, 100, 200, 500, or 1000")
    if status_filter not in ["all", "enabled", "disabled"]:
        raise HTTPException(status_code=400, detail="status_filter can only be all, enabled, or disabled")
    if cooldown_filter and cooldown_filter not in ["all", "in_cooldown", "no_cooldown"]:
        raise HTTPException(status_code=400, detail="cooldown_filter can only be all, in_cooldown, or no_cooldown")
    if tier_filter and tier_filter not in ["all", "free", "pro", "ultra"]:
        raise HTTPException(status_code=400, detail="tier_filter can only be all, free, pro, or ultra")

    database = await get_database()
    result = await database.get_credentials_summary(
        offset=offset,
        limit=limit,
        status_filter=status_filter,
        error_code_filter=error_code_filter if error_code_filter and error_code_filter != "all" else None,
        cooldown_filter=cooldown_filter if cooldown_filter and cooldown_filter != "all" else None,
        tier_filter=tier_filter if tier_filter and tier_filter != "all" else None
    )

    return JSONResponse(content={
        "items": result["items"],
        "total": result["total"],
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < result["total"],
        "stats": result.get("stats", {"total": 0, "normal": 0, "disabled": 0}),
    })


async def download_all_creds_common() -> Response:
    """Package and export all credentials (standard format, filename=email.json)."""
    zip_filename = "antigravity_credentials.zip"

    database = await get_database()
    emails = await database.list_credentials()

    if not emails:
        raise HTTPException(status_code=404, detail="No credentials found")

    log.info(f"Starting to package {len(emails)} credentials...")

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        success_count = 0
        for email in emails:
            try:
                state = await database.get_credential_state(email)
                # Get refresh_token / access_token / expiry from credential data
                cred = await database.get_credential(email)
                if not cred:
                    continue

                export_data = {
                    "refresh_token": cred.get("refresh_token", ""),
                    "access_token": cred.get("access_token", ""),
                    "expiry": cred.get("expiry", ""),
                    "email": email,
                    "tier": state.get("tier", "pro"),
                    "project_id": state.get("project_id", ""),
                }

                content = json.dumps(export_data, ensure_ascii=False, indent=2)
                zip_file.writestr(f"{email}.json", content)
                success_count += 1
            except Exception as e:
                log.warning(f"Error exporting credential {email}: {e}")
                continue

    log.info(f"Packaging complete: successfully processed {success_count}/{len(emails)} credentials")

    zip_buffer.seek(0)
    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_filename}"},
    )


async def verify_credential_project_common(email: str) -> JSONResponse:
    """Verify and re-fetch credential project id / tier."""

    database = await get_database()

    credential_data = await database.get_credential(email)
    if not credential_data:
        raise HTTPException(status_code=404, detail="Credential does not exist")

    credentials = Credentials.from_dict(credential_data)

    token_refreshed = await credentials.refresh_if_needed()

    if token_refreshed:
        log.info(f"Token automatically refreshed: {email}")
        credential_data = credentials.to_dict()
        await database.store_credential(email, credential_data)

    # Re-fetch project id / tier
    api_base_url = await get_antigravity_api_url()
    project_id, subscription_tier = await fetch_project_id_and_tier(
        access_token=credentials.access_token,
        user_agent=ANTIGRAVITY_USER_AGENT,
        api_base_url=api_base_url,
    )

    # If project_id fetch fails, fall back to avoid storing null values
    if not project_id:
        log.warning(f"Verify credential {email}: project_id fetch failed, using fallback value aicode-consumers")
        project_id = "aicode-consumers"

    # If tier fetch fails, keep existing database value instead of overwriting with None
    if not subscription_tier:
        old_state = await database.get_credential_state(email)
        subscription_tier = old_state.get("tier") or "pro"
        log.warning(f"Verify credential {email}: tier fetch failed, keeping original value {subscription_tier}")

    credential_data["project_id"] = project_id

    await database.store_credential(email, credential_data)

    state_update = {
        "disabled": False,
        "error_codes": [],
        "tier": subscription_tier,
        "project_id": project_id,
    }

    await database.update_credential_state(email, state_update)

    log.info(f"Credential verification successful: {email} - Project ID: {project_id}, Tier: {subscription_tier}")

    return JSONResponse(content={
        "success": True,
        "email": email,
        "project_id": project_id,
        "subscription_tier": subscription_tier,
        "message": "Verification successful! Disabled status cleared and error codes reset",
    })


# =============================================================================
# Route Handlers
# =============================================================================


@router.post("/upload")
async def upload_credentials( files: List[UploadFile] = File(...),
    token: str = Depends(verify_panel_token),
):
    """Batch import credential files (standard JSON / ZIP)."""
    try:
        return await upload_credentials_common(files)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Batch import failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_creds_status(
    token: str = Depends(verify_panel_token),
    offset: int = 0,
    limit: int = 50,
    status_filter: str = "all",
    error_code_filter: str = "all",
    cooldown_filter: str = "all",
    tier_filter: str = "all"):
    """Get credential status (lightweight summary, supports pagination and status filtering)."""
    try:
        return await get_creds_status_common(
            offset, limit, status_filter,
            error_code_filter=error_code_filter,
            cooldown_filter=cooldown_filter,
            tier_filter=tier_filter
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to get credential status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/action")
async def creds_action(
    request: CredFileActionRequest,
    token: str = Depends(verify_panel_token)):
    """Perform action on credential (enable/disable/delete)."""
    try:

        email = request.email
        action = request.action

        log.info(f"Performing action '{action}' on credential: {email}")

        database = await get_database()

        if action == "enable":
            result = await set_cred_disabled(email, False)
            if result:
                return JSONResponse(content={"message": f"Credential enabled: {email}"})
            raise HTTPException(status_code=500, detail="Failed to enable credential (may not exist)")

        elif action == "disable":
            result = await set_cred_disabled(email, True)
            if result:
                return JSONResponse(content={"message": f"Credential disabled: {email}"})
            raise HTTPException(status_code=500, detail="Failed to disable credential (may not exist)")

        elif action == "delete":
            success = await remove_credential(email)
            if success:
                return JSONResponse(content={"message": f"Credential deleted: {email}"})
            raise HTTPException(status_code=500, detail="Failed to delete credential")

        else:
            raise HTTPException(status_code=400, detail="Invalid action type")

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Credential action failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch-action")
async def creds_batch_action(
    request: CredFileBatchActionRequest,
    token: str = Depends(verify_panel_token)):
    """Batch perform action on credentials (enable/disable/delete)."""
    try:

        action = request.action
        emails = request.emails

        if not emails:
            raise HTTPException(status_code=400, detail="Credential list cannot be empty")

        log.info(f"Executing batch action '{action}' on {len(emails)} credentials")

        success_count = 0
        errors = []

        for email in emails:
            try:
                if action == "enable":
                    await set_cred_disabled(email, False)
                    success_count += 1
                elif action == "disable":
                    await set_cred_disabled(email, True)
                    success_count += 1
                elif action == "delete":
                    if await remove_credential(email):
                        success_count += 1
                    else:
                        errors.append(f"{email}: Failed to delete")
                        continue
                else:
                    errors.append(f"{email}: Invalid action type")
                    continue
            except Exception as e:
                log.error(f"Error processing {email}: {e}")
                errors.append(f"{email}: Processing failed - {str(e)}")
                continue

        result_message = f"Batch action complete: successfully processed {success_count}/{len(emails)} credentials"
        if errors:
            result_message += "\nError details:\n" + "\n".join(errors)

        return JSONResponse(content={
            "success_count": success_count,
            "total_count": len(emails),
            "errors": errors,
            "message": result_message,
        })

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Batch credential action failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export/{email}")
async def export_credential(
    email: str,
    token: str = Depends(verify_panel_token)):
    """Export single credential (standard format, filename=email.json)."""
    try:
        database = await get_database()

        cred = await database.get_credential(email)
        if not cred:
            raise HTTPException(status_code=404, detail="Credential does not exist")

        state = await database.get_credential_state(email)

        export_data = {
            "refresh_token": cred.get("refresh_token", ""),
            "access_token": cred.get("access_token", ""),
            "expiry": cred.get("expiry", ""),
            "email": email,
            "tier": state.get("tier", "pro"),
            "project_id": cred.get("project_id", ""),
        }

        content = json.dumps(export_data, ensure_ascii=False, indent=2)

        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{email}.json"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to export credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download-all")
async def download_all_creds(
    token: str = Depends(verify_panel_token)):
    """Package and export all credentials (each file as email.json)."""
    try:
        return await download_all_creds_common()
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to package export credentials: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verify-project/{email}")
async def verify_credential_project(
    email: str,
    token: str = Depends(verify_panel_token)):
    """Verify credential (re-fetch Project ID / Tier, can fix 403)."""
    try:
        return await verify_credential_project_common(email)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to verify credential {email}: {e}")
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")


@router.get("/errors/{email}")
async def get_credential_errors(
    email: str,
    token: str = Depends(verify_panel_token)):
    """Get credential error information."""
    try:
        database = await get_database()
        error_info = await database.get_credential_errors(email)
        return JSONResponse(content=error_info)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to get credential error information {email}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/quota/{email}")
async def get_credential_quota(
    email: str,
    token: str = Depends(verify_panel_token)):
    """Get credential quota information."""
    try:
        database = await get_database()

        credential_data = await database.get_credential(email)
        if not credential_data:
            raise HTTPException(status_code=404, detail="Credential does not exist")

        creds = Credentials.from_dict(credential_data)

        await creds.refresh_if_needed()

        updated_data = creds.to_dict()
        if updated_data != credential_data:
            log.info(f"Token automatically refreshed: {email}")
            await database.store_credential(email, updated_data)
            credential_data = updated_data

        access_token = credential_data.get("access_token") or credential_data.get("token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token found in credential")

        quota_info = await fetch_quota_info(access_token, credential_data.get("project_id", ""))

        if quota_info.get("success"):
            result = {
                "success": True,
                "email": email,
                "models": quota_info.get("models", {})
            }
            for extra_key in ("minResetDays", "accountTier", "isPro", "quotaGroups", "source"):
                if extra_key in quota_info:
                    result[extra_key] = quota_info[extra_key]
            return JSONResponse(content=result)
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "email": email,
                    "error": quota_info.get("error", "Unknown error")
                }
            )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to get credential quota {email}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get quota: {str(e)}")


@router.post("/test/{email}")
async def test_credential(
    email: str,
    _token: str = Depends(verify_panel_token)
):
    """
    Test whether credential is valid

    Returns:
        - 200: Credential is valid
        - 429: Credential is rate limited but valid
        - Other: Credential failed (returns actual error code)
    """
    try:
        database = await get_database()

        credential_data = await database.get_credential(email)
        if not credential_data:
            raise HTTPException(status_code=404, detail="Credential does not exist")

        credentials = Credentials.from_dict(credential_data)
        token_refreshed = await credentials.refresh_if_needed()

        if token_refreshed:
            log.info(f"Token automatically refreshed: {email}")
            credential_data = credentials.to_dict()
            await database.store_credential(email, credential_data)

        access_token = credential_data.get("access_token") or credential_data.get("token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token found in credential")

        project_id = credential_data.get("project_id", "")
        if not project_id:
            raise HTTPException(status_code=400, detail="No project ID found in credential")

        test_model = "gemini-2.5-flash-lite"

        api_base_url = await get_antigravity_api_url()
        headers = build_antigravity_headers(access_token)

        response = await post_async(
            url=f"{api_base_url}/v1internal:generateContent",
            json={
                "model": test_model,
                "project": project_id,
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}]
                }
            },
            headers=headers,
            timeout=30.0
        )

        status_code = response.status_code

        if status_code == 200 or status_code == 429:
            log.info(f"Credential test successful: {email} (status={status_code})")
            if status_code == 200:
                await database.update_credential_state(email, {
                    "error_codes": [],
                    "error_messages": {}
                })

            return JSONResponse(
                status_code=status_code,
                content={
                    "success": True,
                    "status_code": status_code,
                    "message": "Test successful",
                    "email": email
                }
            )
        else:
            log.warning(f"Credential test failed: {email} (status={status_code})")
            try:
                error_text = response.text if hasattr(response, "text") else ""
                error_codes = [status_code]
                error_messages = {str(status_code): error_text if error_text else f"HTTP {status_code}"}

                await database.update_credential_state(email, {
                    "error_codes": error_codes,
                    "error_messages": error_messages
                })
            except Exception as e:
                log.error(f"Failed to save test error information: {e}")

        error_text = response.text if hasattr(response, "text") else ""

        return JSONResponse(
            status_code=status_code,
            content={
                "success": False,
                "status_code": status_code,
                "message": f"Test failed: HTTP {status_code}",
                "error": error_text,
                "email": email
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to test credential {email}: {e}")
        raise HTTPException(status_code=500, detail=f"Test failed: {str(e)}")
