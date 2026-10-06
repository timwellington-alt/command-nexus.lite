"""Dashboard card registry (lite build).

Each card has:
  - id            stable string used in user.dashboard_layout JSON
  - label         display label
  - permission    required action (None = visible to any authenticated user)
  - fetcher       async (db, user) -> dict, returns the card's data payload
                  (or None to indicate "data unavailable, show empty state")

Adding a card = define a fetcher + add it to CARDS. The frontend has a
matching client-side renderer keyed by card id (see dashboard.html).

The full the district build ships ~25 cards spanning network/security/voice/
chromebook/etc. The lite build strips those and keeps the trio that
apply to any Staff+Roster deployment.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User

logger = logging.getLogger(__name__)


async def _fetch_staff_count(db: AsyncSession, user: User) -> dict | None:
    try:
        row = (await db.execute(text(
            "SELECT COUNT(*) AS n FROM staff_directory"
        ))).mappings().first()
        return {"total": int(row["n"]) if row else 0}
    except Exception as e:
        logger.warning(f"staff_count card: {e}")
        return None


async def _fetch_roster_count(db: AsyncSession, user: User) -> dict | None:
    try:
        row = (await db.execute(text(
            "SELECT COUNT(*) AS n FROM roster_snapshots WHERE status = 'active'"
        ))).mappings().first()
        return {"total": int(row["n"]) if row else 0}
    except Exception as e:
        logger.warning(f"roster_count card: {e}")
        return None


async def _fetch_recent_activity(db: AsyncSession, user: User) -> dict | None:
    try:
        rows = (await db.execute(text("""
            SELECT actor, action, target, created_at
            FROM audit_logs
            ORDER BY id DESC LIMIT 10
        """))).mappings().all()
        return {
            "events": [
                {
                    "actor": r["actor"],
                    "action": r["action"],
                    "target": (r["target"] or "")[:80],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
        }
    except Exception as e:
        logger.warning(f"recent_activity card: {e}")
        return None


# ── Registry ──────────────────────────────────────────────────────────────

CARDS: list[dict[str, Any]] = [
    {
        "id": "staff_count",
        "label": "Staff",
        "permission": "staff.view",
        "fetcher": _fetch_staff_count,
    },
    {
        "id": "roster_count",
        "label": "Active Students",
        "permission": "roster.view",
        "fetcher": _fetch_roster_count,
    },
    {
        "id": "recent_activity",
        "label": "Recent Activity",
        "permission": "audit.view",
        "fetcher": _fetch_recent_activity,
    },
]


def get_card(card_id: str) -> dict | None:
    for c in CARDS:
        if c["id"] == card_id:
            return c
    return None
