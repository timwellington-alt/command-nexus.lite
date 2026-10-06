"""
Audit service — records all sensitive actions to the audit log.

Every module uses this service for durable, structured audit records.
Automatically picks up the request correlation ID when available.

`student_data_access:*` actions are rate-limited in-process so that
auto-refreshing UI panels (dashboard, guidance tab) don't spam the
audit log with one row per minute for every open tab. The FERPA
requirement is satisfied with one entry per (actor, action, target)
per dedup window — subsequent hits inside the window are suppressed.
"""

import logging
import time
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.audit.correlation import get_correlation_id

logger = logging.getLogger(__name__)

# In-memory dedup cache for student_data_access events.
# Maps (actor, action, target) -> expiry epoch seconds.
# Cleaned lazily on each insert so it never grows unbounded.
_STUDENT_ACCESS_DEDUP: dict[tuple[str, str, str], float] = {}
_STUDENT_ACCESS_TTL_SECONDS = 15 * 60  # 15 minutes
_STUDENT_ACCESS_PREFIX = "student_data_access:"


def _student_access_should_skip(actor: str, action: str, target: str | None) -> bool:
    """
    Rate-limit student_data_access events to one row per
    (actor, action, target) every _STUDENT_ACCESS_TTL_SECONDS.

    Returns True if the event is within an active dedup window
    and should be dropped. Returns False (and records the new
    window) otherwise.
    """
    if not action.startswith(_STUDENT_ACCESS_PREFIX):
        return False

    key = (actor or "", action, target or "")
    now = time.time()

    # Lazy cleanup of expired entries — cheap and keeps the dict small
    if len(_STUDENT_ACCESS_DEDUP) > 256:
        expired = [k for k, exp in _STUDENT_ACCESS_DEDUP.items() if exp <= now]
        for k in expired:
            _STUDENT_ACCESS_DEDUP.pop(k, None)

    exp = _STUDENT_ACCESS_DEDUP.get(key)
    if exp and exp > now:
        return True

    _STUDENT_ACCESS_DEDUP[key] = now + _STUDENT_ACCESS_TTL_SECONDS
    return False


async def log_action(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    module: str,
    target: str | None = None,
    outcome: str = "success",
    details: str | None = None,
    ip_address: str | None = None,
    correlation_id: str | None = None,
) -> AuditLog | None:
    """
    Stage an audit event. Returns the created record, or None if the
    event was suppressed by the student_data_access dedup filter.

    Does NOT commit — the caller owns the transaction boundary.
    Uses flush() so the record gets an ID for return, but the
    transaction remains open for the caller to commit or rollback.

    If no correlation_id is provided, automatically uses the current
    request's correlation ID from context.

    Callers that rely on the returned AuditLog instance (e.g. to read
    `entry.id`) must handle the None case when the action starts with
    `student_data_access:`. All other actions always return an AuditLog.
    """
    # FERPA-style student data access events get deduped in-process so
    # auto-refreshing UI panels don't spam the log.
    if _student_access_should_skip(actor, action, target):
        logger.debug(
            f"audit: dedup skip {action} by {actor} on {target or '-'}"
        )
        return None

    cid = correlation_id or get_correlation_id()

    entry = AuditLog(
        actor=actor,
        action=action,
        target=target,
        module=module,
        outcome=outcome,
        details=details,
        ip_address=ip_address,
        correlation_id=cid,
    )
    db.add(entry)
    await db.flush()

    logger.info(
        f"audit: {action} by {actor} on {target or '-'} [{module}] → {outcome}",
    )

    return entry
