"""
Job status API — exposes worker job results and failures.

Failed jobs are NEVER silent. This endpoint makes them visible.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from app.db.models import User
from app.policies.engine import require_action

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/status")
async def job_status(
    limit: int = 50,
    user: User = Depends(require_action("settings.manage")),
):
    """
    Show recent job results and failures from the worker.
    Admin only — requires settings.manage permission.

    Returns 200 with job list on success.
    Returns 503 if the status query itself fails — a broken endpoint
    that returns 200 with empty jobs defeats the purpose of visibility.
    """
    from app.workers.status import get_recent_jobs
    try:
        jobs = await get_recent_jobs(limit=limit)
        failed = [j for j in jobs if not j.get("success")]
        return {
            "total": len(jobs),
            "failed": len(failed),
            "jobs": jobs,
        }
    except Exception as e:
        logger.error(f"Failed to query job status: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "error": "Job status unavailable",
                "detail": str(e),
            },
        )
