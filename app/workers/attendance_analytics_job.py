"""Nightly attendance analytics + spike alert.

Runs three tasks in one pass:

  1. Rebuild `student_analytics` (per-student chronic-absence flag).
     Ohio Chronic Absenteeism = missed 10%+ of enrolled days YTD (any
     absence type counted).

  2. Rebuild `attendance_daily_stats` for the last N days (default 60
     so a re-run heals any prior gap without reprocessing history).
     One row per (date, school_code): total absences, distinct
     students absent, enrolled count, rate percent.

  3. Compare TODAY's per-school absence rate to a rolling 30-day
     baseline (weighted mean, excluding today). If today's rate
     exceeds the baseline by `guidance.absence_spike_threshold_pct`
     (default 50%) AND today's absence count is at least
     `guidance.absence_spike_min_absences` (default 5, to avoid
     paging on tiny cohorts), dispatch a routed alert via the same
     `dispatch_routed_alert` path as the printer/ACU/lightning jobs.

Dedupe: Redis marker `attendance:spike_alerted:{school}:{yyyy-mm-dd}`
so a given school-day can only alert once. Cleared automatically the
next day because the key ttl is 48h.

Cadence: daily. Best fires overnight after the day's absences have
propagated + Clever CSV has landed (poll_clever_imports runs 2 am
locally). Scheduler entry at ~03:00 local equivalent.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


_ALERT_KEY = "attendance:spike_alerted:"
_ALERT_TTL_SECONDS = 48 * 3600


def _school_year_start(today: date) -> date:
    """Aug 1 anchor. If we're before August, use last year's Aug 1."""
    return date(today.year if today.month >= 8 else today.year - 1, 8, 1)


async def _redis():
    import redis.asyncio as aioredis
    from app.config import get_settings
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def _spike_threshold_pct(db: AsyncSession) -> float:
    from app.modules.settings.repository import get_setting_value
    raw = (await get_setting_value(db, "guidance", "absence_spike_threshold_pct") or "").strip()
    try:
        n = float(raw)
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    return 50.0


async def _spike_min_absences(db: AsyncSession) -> int:
    from app.modules.settings.repository import get_setting_value
    raw = (await get_setting_value(db, "guidance", "absence_spike_min_absences") or "").strip()
    try:
        n = int(raw)
        if n >= 1:
            return n
    except (TypeError, ValueError):
        pass
    return 5


async def snapshot_attendance_analytics(ctx: dict) -> dict:
    """Rebuild the two analytics tables + emit spike alerts."""
    from app.db.engine import AsyncSessionLocal
    from app.modules.alerts.service import dispatch_routed_alert
    from app.modules.settings.buildings import (
        get_building_maps, resolve_building_code_sync,
    )

    result = {
        "job": "snapshot_attendance_analytics",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "student_rows_written": 0,
        "chronic_count": 0,
        "daily_rows_written": 0,
        "spike_schools_checked": 0,
        "spike_alerts_fired": 0,
        "spike_alerts_suppressed": 0,
        "errors": [],
    }

    try:
        async with AsyncSessionLocal() as db:
            today = date.today()
            sy_start = _school_year_start(today)
            days_enrolled = (today - sy_start).days + 1

            # Load the SIS→internal building map once per run and
            # canonicalize every school_code we're about to write.
            # Analytics tables should hold internal abbreviations
            # (PES/PHS/EPE), never raw SIS codes (SIS_A/SIS_B/SIS_C) —
            # matches the convention in app/modules/settings/buildings.py.
            bmap = await get_building_maps(db)
            def _canon(sc: str | None) -> str | None:
                return resolve_building_code_sync(sc, bmap) or sc

            # ── 1. student_analytics — chronic flag per active student ──
            # Absences numerator: count of student_absences rows within
            # the school year, any type. Denominator: days_enrolled
            # (calendar approximation — using enrolled school days
            # would need a district calendar; calendar days give a
            # slightly lower rate that's still directionally right and
            # trivially derivable. Refine when a school-day calendar
            # feed is available).
            rewrite = await db.execute(text("""
                WITH counts AS (
                    SELECT sis_id, COUNT(*) AS n_abs
                    FROM student_absences
                    WHERE calendar_date >= :sy_start
                    GROUP BY sis_id
                )
                INSERT INTO student_analytics
                    (sis_id, school_code, grade,
                     days_absent_ytd, days_enrolled_ytd,
                     absence_rate_pct, chronic_absent, computed_at)
                SELECT
                    rs.sis_id,
                    rs.school,
                    rs.grade,
                    COALESCE(c.n_abs, 0),
                    :days_enrolled,
                    ROUND(100.0 * COALESCE(c.n_abs, 0) / GREATEST(:days_enrolled, 1), 2),
                    (COALESCE(c.n_abs, 0) * 100.0
                        / GREATEST(:days_enrolled, 1)) >= 10.0,
                    NOW()
                FROM roster_snapshots rs
                LEFT JOIN counts c ON c.sis_id = rs.sis_id
                WHERE rs.status = 'active'
                ON CONFLICT (sis_id) DO UPDATE SET
                    school_code = EXCLUDED.school_code,
                    grade = EXCLUDED.grade,
                    days_absent_ytd = EXCLUDED.days_absent_ytd,
                    days_enrolled_ytd = EXCLUDED.days_enrolled_ytd,
                    absence_rate_pct = EXCLUDED.absence_rate_pct,
                    chronic_absent = EXCLUDED.chronic_absent,
                    computed_at = NOW()
            """).bindparams(sy_start=sy_start, days_enrolled=days_enrolled))
            result["student_rows_written"] = rewrite.rowcount or 0

            # Canonicalize the raw SIS codes we just wrote → internal
            # abbreviations. One UPDATE per SIS code — trivially small
            # (a handful of distinct values). Same pattern applied to
            # attendance_daily_stats below.
            for sis_code, internal in bmap["sis_to_internal"].items():
                if sis_code == internal:
                    continue
                await db.execute(text(
                    "UPDATE student_analytics SET school_code = :i WHERE school_code = :s"
                ).bindparams(i=internal, s=sis_code))

            chronic_row = (await db.execute(text(
                "SELECT COUNT(*) FROM student_analytics WHERE chronic_absent"
            ))).scalar_one()
            result["chronic_count"] = int(chronic_row)

            # ── 2. attendance_daily_stats — last 60 days, per school ──
            #
            # `enrolled_count` is a snapshot of currently-active roster
            # rows per school. Approximation: assumes school membership
            # is stable over the 60-day window. Good enough for a
            # short trend; the full-year chart will drift ~1-2% for
            # kids who transferred mid-year — noted for Phase D.
            window_days = 60
            window_start = today - timedelta(days=window_days)

            # Both maps are keyed on the canonical internal code so a
            # SIS-only day (e.g. absence rows still labelled SIS_A)
            # rolls up under the same key as the enrolled count for
            # PES.
            enrolled_raw = (await db.execute(text("""
                SELECT school, COUNT(*)
                FROM roster_snapshots
                WHERE status = 'active'
                GROUP BY school
            """))).all()
            enrolled: dict[str, int] = {}
            for school, cnt in enrolled_raw:
                key = _canon(school)
                if not key:
                    continue
                enrolled[key] = enrolled.get(key, 0) + int(cnt)

            per_day_school = (await db.execute(text("""
                SELECT calendar_date, school_code,
                       COUNT(*) AS n_abs,
                       COUNT(DISTINCT sis_id) AS n_students
                FROM student_absences
                WHERE calendar_date >= :start
                GROUP BY calendar_date, school_code
            """).bindparams(start=window_start))).all()

            for cd, sc_raw, n_abs, n_students in per_day_school:
                sc = _canon(sc_raw) or sc_raw
                enr = enrolled.get(sc, 0)
                rate = round(100.0 * int(n_students) / enr, 2) if enr else 0.0
                await db.execute(text("""
                    INSERT INTO attendance_daily_stats
                        (calendar_date, school_code, total_absences,
                         distinct_students_absent, enrolled_count,
                         absence_rate_pct, computed_at)
                    VALUES (:d, :s, :n, :ns, :enr, :rate, NOW())
                    ON CONFLICT (calendar_date, school_code) DO UPDATE SET
                        total_absences = EXCLUDED.total_absences,
                        distinct_students_absent = EXCLUDED.distinct_students_absent,
                        enrolled_count = EXCLUDED.enrolled_count,
                        absence_rate_pct = EXCLUDED.absence_rate_pct,
                        computed_at = NOW()
                """).bindparams(
                    d=cd, s=sc, n=int(n_abs), ns=int(n_students),
                    enr=enr, rate=rate,
                ))
                result["daily_rows_written"] += 1

            await db.commit()

            # ── 3. Spike alerts — today vs 30-day baseline per school ──
            threshold_pct = await _spike_threshold_pct(db)
            min_absences = await _spike_min_absences(db)
            baseline_start = today - timedelta(days=30)

            today_rows = (await db.execute(text("""
                SELECT school_code, total_absences, distinct_students_absent,
                       enrolled_count, absence_rate_pct
                FROM attendance_daily_stats
                WHERE calendar_date = :today
            """).bindparams(today=today))).mappings().all()
            result["spike_schools_checked"] = len(today_rows)

            r = await _redis()
            try:
                for row in today_rows:
                    school = row["school_code"]
                    today_rate = float(row["absence_rate_pct"] or 0)
                    today_absences = int(row["total_absences"] or 0)
                    if today_absences < min_absences:
                        continue

                    baseline = (await db.execute(text("""
                        SELECT AVG(absence_rate_pct)
                        FROM attendance_daily_stats
                        WHERE school_code = :sc
                          AND calendar_date >= :start
                          AND calendar_date < :today
                          AND absence_rate_pct > 0
                    """).bindparams(sc=school, start=baseline_start, today=today))).scalar()
                    if baseline is None or float(baseline) <= 0:
                        # Fresh school with no prior data — no baseline
                        # to compare against yet. Skip cleanly.
                        continue
                    baseline = float(baseline)
                    delta_pct = 100.0 * (today_rate - baseline) / baseline
                    if delta_pct < threshold_pct:
                        continue

                    dedup_key = f"{_ALERT_KEY}{school}:{today.isoformat()}"
                    if await r.exists(dedup_key):
                        result["spike_alerts_suppressed"] += 1
                        continue

                    summary = (
                        f"Attendance spike at {school}: today's absence rate "
                        f"{today_rate:.1f}% is +{delta_pct:.0f}% vs the 30-day "
                        f"baseline of {baseline:.1f}%. "
                        f"{today_absences} total absences across "
                        f"{int(row['distinct_students_absent'])} students "
                        f"(enrolled {int(row['enrolled_count'])}). "
                        f"Possible bug going around."
                    )
                    await r.set(dedup_key, int(datetime.now(timezone.utc).timestamp()),
                                ex=_ALERT_TTL_SECONDS)
                    try:
                        await dispatch_routed_alert(
                            db,
                            building_code=school,
                            severity="medium",
                            source_module="attendance",
                            source_ref=f"attendance_spike:{school}:{today.isoformat()}",
                            summary=summary,
                            affected_count=int(row["distinct_students_absent"]),
                        )
                        await db.commit()
                        result["spike_alerts_fired"] += 1
                        logger.warning(f"Attendance spike alert dispatched: {summary}")
                    except Exception as e:
                        await r.delete(dedup_key)
                        logger.error(f"Attendance spike alert dispatch failed for {school}: {e}")
                        result["errors"].append(f"{school}: {str(e)[:120]}")
            finally:
                await r.aclose()

    except Exception as e:
        logger.exception("snapshot_attendance_analytics failed")
        result["errors"].append(f"{type(e).__name__}: {e}"[:200])
        return result

    logger.info(f"attendance analytics: {json.dumps({k: v for k, v in result.items() if k != 'errors'})}")
    return result
