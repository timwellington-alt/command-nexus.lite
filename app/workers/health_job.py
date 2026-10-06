"""
Health check job — observable worker test.

Verifies worker→Redis→DB connectivity and logs the result.
Used as the baseline job for T5.1 and T5.2.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def health_check_job(ctx: dict) -> dict:
    """
    Worker health check — tests DB and Redis connectivity.
    Result stored in ARQ's job result store for visibility via T5.3.

    ctx['redis'] is the live ARQ Redis pool — no new connection needed.
    """
    from app.db.engine import AsyncSessionLocal
    import sqlalchemy as sa

    result = {
        "job": "health_check",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "db_ok": False,
        "redis_ok": False,
    }

    # Real Redis check via the ARQ pool already in context
    try:
        await ctx['redis'].ping()
        result['redis_ok'] = True
    except Exception as e:
        result['redis_error'] = str(e)
        logger.error(f"Health check Redis failure: {e}")

    # DB check
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(sa.text("SELECT 1"))
            result["db_ok"] = True
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Health check DB failure: {e}")

    status = "healthy" if result["db_ok"] and result["redis_ok"] else "unhealthy"
    logger.info(f"Worker health check: {status}")
    result["status"] = status

    return result
