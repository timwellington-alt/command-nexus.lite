"""Mark pending onboarding tokens whose expires_at has passed as
``expired``. Runs nightly. Keeps the admin token list clean and
prevents the expiry-check logic in the public form from being the
only enforcement point.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


async def expire_stale_onboarding_tokens(ctx: dict) -> dict:
    from app.db.engine import AsyncSessionLocal

    result = {
        "job": "expire_stale_onboarding_tokens",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "expired": 0,
    }
    try:
        async with AsyncSessionLocal() as db:
            # Cheap sweep — the (status, expires_at) index makes this
            # index-only until enough rows exist to matter.
            res = await db.execute(text(
                "UPDATE onboarding_tokens "
                "SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at < now() "
                "RETURNING id"
            ))
            expired_ids = [r[0] for r in res.fetchall()]
            result["expired"] = len(expired_ids)
            if expired_ids:
                logger.info(
                    f"Onboarding tokens expired: {len(expired_ids)} "
                    f"({expired_ids[:10]}{'...' if len(expired_ids) > 10 else ''})"
                )
            await db.commit()
    except Exception as e:
        logger.exception("expire_stale_onboarding_tokens failed")
        result["error"] = f"{type(e).__name__}: {e}"[:200]
    return result
