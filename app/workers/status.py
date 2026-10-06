"""
Job status service — exposes ARQ job results and failures.

ARQ stores job results in Redis for the duration configured by keep_result.
This module queries that store and exposes it via API.
"""

import logging
from datetime import datetime, timezone

from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus

from app.config import get_settings

logger = logging.getLogger(__name__)


async def get_recent_jobs(limit: int = 50) -> list[dict]:
    """
    Query ARQ's Redis result store for recent job results.
    Returns both successful and failed jobs.

    Uses scan_iter with count hint and breaks early at the limit
    to avoid unbounded iteration over retained results.
    """
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    try:
        results = []
        async for key in redis.scan_iter("arq:result:*", count=100):
            try:
                job_id = key.split(":")[-1] if isinstance(key, str) else key.decode().split(":")[-1]
                job = Job(job_id, redis)
                info = await job.info()
                if info:
                    results.append({
                        "job_id": job_id,
                        "function": info.function,
                        "status": info.status.value if hasattr(info.status, 'value') else str(info.status),
                        "enqueue_time": info.enqueue_time.isoformat() if info.enqueue_time else None,
                        "start_time": info.start_time.isoformat() if info.start_time else None,
                        "finish_time": info.finish_time.isoformat() if info.finish_time else None,
                        "success": info.success,
                        "result": str(info.result)[:500] if info.result is not None else None,
                        "error": str(info.result)[:500] if not info.success and info.result else None,
                    })
            except Exception as e:
                logger.debug(f"Could not read job {key}: {e}")

            # Stop scanning once we have enough candidates
            if len(results) >= limit * 2:
                break

        # Sort by most recent first
        results.sort(key=lambda x: x.get("finish_time") or x.get("enqueue_time") or "", reverse=True)
        return results[:limit]

    finally:
        await redis.close()
