"""
Request log route module - Handles HTTP requests related to /usage-logs/*
Query usage_log table: paginated list, aggregate statistics, cleanup, request body detail log reading
"""

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from config import get_usage_log_enabled
from log import log
from src.request_body_log import clear_request_body_logs, read_request_body_log
from src.storage.database import UsageLog, get_database
from src.usage_log import clear_usage_logs, flush_usage_logs
from src.utils import verify_panel_token


# Create router
router = APIRouter(prefix="/usage-logs", tags=["usage-logs"])


@router.get("/list")
async def get_usage_logs(
    token: str = Depends(verify_panel_token),
    offset: int = 0,
    limit: int = 50,
    start_timestamp: Optional[float] = None,
    end_timestamp: Optional[float] = None,
    model_name: str = "",
    credential_email: str = "",
    api_format: str = "",
    status: str = "all",
    is_stream: Optional[bool] = None,
    status_code: Optional[int] = None,
):
    """Paginated query of request logs (newest -> oldest)."""
    try:
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset cannot be negative")

        database = await get_database()
        # Flush pending logs in the batch write queue to disk first to ensure the panel sees the latest data
        await flush_usage_logs()
        items, total = await database.query_usage_logs(
            offset=offset,
            limit=limit,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            model_name=model_name.strip(),
            credential_email=credential_email.strip(),
            api_format=api_format.strip(),
            status=status,
            is_stream=is_stream,
            status_code=status_code,
        )
        return JSONResponse(content={"items": items, "total": total})
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to query request logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_usage_stats(
    token: str = Depends(verify_panel_token),
    start_timestamp: Optional[float] = None,
    end_timestamp: Optional[float] = None,
    model_name: str = "",
    credential_email: str = "",
    api_format: str = "",
):
    """Aggregate statistics: request count / success & failure / token summary / average duration / distribution per model."""
    try:
        database = await get_database()
        # Flush pending logs in the batch write queue to disk first to ensure complete statistics
        await flush_usage_logs()
        stats = await database.usage_log_stats(
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            model_name=model_name.strip(),
            credential_email=credential_email.strip(),
            api_format=api_format.strip(),
        )
        return JSONResponse(content=stats)
    except Exception as e:
        log.error(f"Failed to query log statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clear")
async def clear_logs(
    token: str = Depends(verify_panel_token),
    days: int = 30,
):
    """Clean up logs older than N days, return deleted count."""
    try:
        if days < 0:
            raise HTTPException(status_code=400, detail="Days cannot be negative")
        if days == 0:
            # 0 = delete all (cutoff = now), simultaneously clear detailed request body log files
            deleted = await clear_usage_logs(0)
            files_deleted = await asyncio.to_thread(clear_request_body_logs)
            return JSONResponse(content={
                "message": "All logs cleared",
                "deleted": deleted,
                "files_deleted": files_deleted,
            })
        deleted = await clear_usage_logs(days)
        # Simultaneously delete detailed request body log files older than N days
        files_deleted = await asyncio.to_thread(
            clear_request_body_logs, time.time() - days * 86400
        )
        return JSONResponse(content={
            "message": f"Cleared logs older than {days} days",
            "deleted": deleted,
            "files_deleted": files_deleted,
        })
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to clear logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/truncate")
async def truncate_logs(token: str = Depends(verify_panel_token)):
    """Clear logs: directly DROP the entire usage_log table and recreate it, leaving no residue."""
    try:
        database = await get_database()
        # Flush pending logs in the batch write queue first
        await flush_usage_logs()
        # Count current records first for response
        _, total = await database.query_usage_logs(offset=0, limit=1)

        async with database.session() as s:
            await s.execute(text(f"DROP TABLE IF EXISTS {UsageLog.__tablename__}"))

        # Recreate table (using unified table creation logic to ensure latest schema)
        async with database._engine.begin() as conn:
            await conn.run_sync(database._create_tables)

        # Simultaneously clear all detailed request body log files
        files_deleted = await asyncio.to_thread(clear_request_body_logs)

        return JSONResponse(content={
            "message": "All logs cleared (table recreated)",
            "deleted": total,
            "files_deleted": files_deleted,
        })
    except Exception as e:
        log.error(f"Failed to clear logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/request-body")
async def get_request_body_log(
    path: str,
    token: str = Depends(verify_panel_token),
):
    """Read detailed request body log (JSON) from disk by file index."""
    try:
        data = await asyncio.to_thread(read_request_body_log, path)
        if data is None:
            raise HTTPException(status_code=404, detail="Detailed log file does not exist or has been cleaned up")
        return JSONResponse(content=data)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to read request body detail log: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_usage_log_config(token: str = Depends(verify_panel_token)):
    """Get log toggle configuration."""
    return JSONResponse(content={
        "enabled": await get_usage_log_enabled(),
    })
