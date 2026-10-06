"""Daily attendance digest email — sends per-building reports.

Runs once daily after snapshot_attendance_analytics. For each active
recipient row, builds today's digest for the recipient's building
(empty building_code = district-wide) and sends via Gmail API.

Per-recipient audit log entry + Redis dedup per (building_code, date)
so a re-run doesn't double-send. Dedup key TTL is 30h; a full day
buffer prevents wall-clock races at midnight.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

_DEDUP_KEY_PREFIX = "attendance:report_sent:"
_DEDUP_TTL_SECONDS = 30 * 3600


async def _redis():
    import redis.asyncio as aioredis
    from app.config import get_settings
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def send_attendance_daily_reports(ctx: dict) -> dict:
    from app.db.engine import AsyncSessionLocal
    from app.modules.roster import attendance_reports as ar
    from app.modules.settings.repository import get_setting_value

    result = {
        "job": "send_attendance_daily_reports",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "buildings_processed": 0,
        "emails_sent": 0,
        "emails_skipped_no_data": 0,
        "emails_deduped": 0,
        "errors": [],
    }

    try:
        async with AsyncSessionLocal() as db:
            # Weekday + HH:MM gate. Scheduler polls every 900s (15 min),
            # so the target minute must be a quarter-hour (0/15/30/45).
            # Manual invocations (job_id not starting "scheduled:")
            # bypass the gate for on-demand fires from the settings page.
            #
            # Settings:
            #   attendance.reports_send_hour    — 0-23 local, default 11
            #   attendance.reports_send_minute  — 0/15/30/45, default 0
            #     (defaults chosen so send lands AFTER the MetaSolutions
            #     "Daily Attendance" email arrives ~10:30 and finishes
            #     ingesting, BEFORE lunch prep peaks in the cafeterias)
            #   attendance.reports_weekdays     — CSV of ints 0=Mon..6=Sun
            #     (default "0,1,2,3,4" — every weekday)
            is_scheduled = ctx.get("job_id", "").startswith("scheduled:")
            if is_scheduled:
                from zoneinfo import ZoneInfo
                tz_name = (await get_setting_value(db, "branding", "timezone")
                           or "America/New_York")
                try:
                    target_hr = int(
                        await get_setting_value(db, "attendance", "reports_send_hour")
                        or "11"
                    )
                except ValueError:
                    target_hr = 11
                try:
                    target_min = int(
                        await get_setting_value(db, "attendance", "reports_send_minute")
                        or "0"
                    )
                except ValueError:
                    target_min = 0
                # Round the target minute DOWN to the nearest 15 so the
                # scheduler's 900s cadence has a matching tick — a bad
                # save like minute=17 shouldn't silently skip forever.
                target_min = (target_min // 15) * 15
                weekdays_raw = (
                    await get_setting_value(db, "attendance", "reports_weekdays")
                    or "0,1,2,3,4"
                )
                try:
                    allowed_wd = {int(x) for x in weekdays_raw.split(",") if x.strip()}
                except ValueError:
                    allowed_wd = {0, 1, 2, 3, 4}
                now_local = datetime.now(ZoneInfo(tz_name))
                # Same 15-min rounding on "now" so a tick at 11:04:07
                # matches a target of 11:00.
                now_min_bucket = (now_local.minute // 15) * 15
                if (now_local.weekday() not in allowed_wd
                        or now_local.hour != target_hr
                        or now_min_bucket != target_min):
                    result["aborted"] = (
                        f"not in scheduled window "
                        f"(weekdays={sorted(allowed_wd)}, "
                        f"time={target_hr:02d}:{target_min:02d}; "
                        f"got weekday={now_local.weekday()}, "
                        f"time={now_local.hour:02d}:{now_min_bucket:02d} {tz_name})"
                    )
                    return result

            today = date.today()
            grouped = await ar.get_active_recipients_grouped(db)
            if not grouped:
                return result

            sender = (await get_setting_value(db, "attendance", "report_sender_email")
                      or ar.DEFAULT_SENDER)
            dashboard = ((await get_setting_value(db, "branding", "public_base_url")
                          or "http://localhost:8000") + "/roster/analytics")

            r = await _redis()
            try:
                for building_code, emails in grouped.items():
                    result["buildings_processed"] += 1
                    dedup_key = f"{_DEDUP_KEY_PREFIX}{building_code}:{today.isoformat()}"
                    if await r.exists(dedup_key):
                        result["emails_deduped"] += len(emails)
                        continue

                    digest = await ar.build_building_digest(
                        db, building_code=building_code, report_date=today,
                    )
                    if digest is None:
                        result["emails_skipped_no_data"] += len(emails)
                        continue

                    subject = (f"[Nexus] Absentees — {digest['display_name']} — "
                               f"{today.isoformat()}")
                    html = ar.render_digest_html(digest, dashboard)

                    for to in emails:
                        try:
                            msg_id = await ar.send_digest_email(sender, to, subject, html)
                            result["emails_sent"] += 1
                            await db.execute(text("""
                                INSERT INTO audit_logs
                                    (actor, action, module, target, details, created_at)
                                VALUES
                                    ('system', 'roster.attendance_reports.digest_sent', 'roster',
                                     :to, CAST(:details AS JSONB), NOW())
                            """).bindparams(
                                to=to,
                                details=json.dumps({
                                    "building_code": building_code,
                                    "report_date": today.isoformat(),
                                    "gmail_message_id": msg_id,
                                    "count": digest["count"],
                                }),
                            ))
                        except Exception as e:
                            logger.exception(f"send digest to {to} failed")
                            result["errors"].append(f"{to}: {type(e).__name__}: {str(e)[:120]}")

                    await db.commit()
                    # Set dedup key only AFTER at least one recipient succeeded — a
                    # transient Gmail outage should not silently skip tomorrow's send.
                    if result["emails_sent"] > 0:
                        await r.set(dedup_key, int(datetime.now(timezone.utc).timestamp()),
                                    ex=_DEDUP_TTL_SECONDS)
            finally:
                await r.aclose()

    except Exception as e:
        logger.exception("send_attendance_daily_reports failed")
        result["errors"].append(f"{type(e).__name__}: {e}"[:200])
        return result

    logger.info(f"attendance reports: {json.dumps({k: v for k, v in result.items() if k != 'errors'})}")
    return result
