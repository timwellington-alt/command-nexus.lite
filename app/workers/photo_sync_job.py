"""
Paxton photo sync job — downloads staff photos from Paxton to local storage.

Photos are served via an authenticated route, never from the static mount.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def sync_paxton_photos(ctx: dict) -> dict:
    """Download all Paxton user photos to local storage."""
    from app.db.engine import AsyncSessionLocal
    from app.integrations.paxton.adapter import PaxtonAdapter

    result = {
        "job": "sync_paxton_photos",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with AsyncSessionLocal() as db:
            paxton = PaxtonAdapter(db)
            sync_result = await paxton.sync_photos()
            result.update(sync_result)
            logger.info(
                f"Photo sync: {sync_result['synced']} downloaded, "
                f"{sync_result['skipped']} skipped, "
                f"{sync_result['failed']} failed"
            )
    except Exception as e:
        logger.error(f"Photo sync failed: {e}")
        result["error"] = str(e)[:200]
        raise

    return result
