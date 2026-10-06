"""
Badge print service — ties the queue entry to the Brother adapter.

Responsibilities:
- Load printer settings (enabled flag + per-building host)
- Render the badge + credentials labels
- Send to the correct building's printer
- Record success/failure on the staff_queue row
- Write an audit entry for every attempt
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


async def print_temp_badge(
    db,
    queue_item: dict,
    temp_password: str,
    actor: str,
) -> dict:
    """
    Print the temp badge + credentials for a provisioned queue entry.

    Always records status on the queue row and always writes an audit
    entry. Never raises — exceptions are captured and returned as an
    error result so the caller's provisioning flow is not disrupted.

    Returns: {"success": bool, "error": str | None, "host": str | None}
    """
    from app.modules.settings.repository import get_setting_value
    from app.audit.service import log_action
    from app.modules.staff.badge_render import render_combined_job
    from app.integrations.brother.adapter import (
        print_images,
        DEFAULT_MODEL,
        DEFAULT_LABEL,
    )

    item_id = queue_item.get("id")

    async def _record(status: str, error: str | None = None, host: str | None = None):
        """Persist status on the queue row + emit an audit log entry."""
        now = datetime.now(timezone.utc)
        await db.execute(
            text(
                """
                UPDATE staff_queue
                SET badge_print_status = :status,
                    badge_print_error = :err,
                    badge_printed_at = :ts
                WHERE id = :id
                """
            ).bindparams(
                status=status, err=error, ts=now if status == "printed" else None, id=item_id,
            )
        )
        try:
            await log_action(
                db,
                actor=actor,
                action=f"staff.badge.print.{status}",
                module="staff",
                target=f"queue#{item_id} {queue_item.get('first_name','')} {queue_item.get('last_name','')}",
                details=json.dumps({
                    "host": host,
                    "error": error,
                    "building": queue_item.get("building"),
                }),
            )
        except Exception as ae:
            logger.warning(f"Badge print audit log failed: {ae}")

    # 1. Is printing enabled at all?
    enabled = (await get_setting_value(db, "staff", "badge_printing_enabled") or "").lower() == "true"
    if not enabled:
        await _record("dismissed", error="badge printing disabled in settings")
        return {"success": False, "error": "disabled", "host": None}

    # 2. Resolve the building's printer host from staff_notifications.
    # Defensive canonicalization: badge printing must survive stale rows
    # that were queued before hr_diff_job started canonicalizing (raw
    # HR names like "the district Elementary School" would look up as
    # "EAST ELEMENTARY" and miss the printer entry).
    # Fall back to the resolver so reprints on legacy rows self-heal.
    notif_raw = await get_setting_value(db, "staff", "staff_notifications") or "{}"
    try:
        notif = json.loads(notif_raw)
    except Exception:
        notif = {}
    raw_building = (queue_item.get("building") or "").strip()
    building = raw_building.upper()
    if building and building not in notif:
        try:
            from app.modules.settings.buildings import (
                get_building_maps, resolve_building_code_sync,
            )
            maps = await get_building_maps(db)
            resolved = resolve_building_code_sync(raw_building, maps)
            if resolved:
                logger.info(
                    f"Badge print: canonicalized {raw_building!r} -> {resolved!r} "
                    f"for queue#{item_id} at lookup time"
                )
                building = resolved.upper()
        except Exception as e:
            logger.warning(f"Badge print building resolver failed: {e}")
    bldg_cfg = notif.get(building) or {}
    host = (bldg_cfg.get("label_printer_host") or "").strip()

    if not host:
        err = f"No label printer configured for building {building or '(none)'}"
        await _record("error", error=err)
        return {"success": False, "error": err, "host": None}

    model = await get_setting_value(db, "staff", "badge_printer_model") or DEFAULT_MODEL
    label_media = await get_setting_value(db, "staff", "badge_label_media") or DEFAULT_LABEL

    # 3. Resolve display data
    district_name = await get_setting_value(db, "branding", "district_name") or ""
    school_names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        school_names = json.loads(school_names_raw)
    except Exception:
        school_names = {}
    building_display = school_names.get(building) or building

    try:
        images = render_combined_job(
            first_name=queue_item.get("first_name") or "",
            last_name=queue_item.get("last_name") or "",
            preferred_name=queue_item.get("preferred_name"),
            email=queue_item.get("expected_email") or queue_item.get("email") or "",
            temp_password=temp_password or "",
            building_name=building_display,
            title=queue_item.get("title") or queue_item.get("position") or "",
            photo_path=queue_item.get("photo_path"),
            district_name=district_name,
        )
    except Exception as e:
        logger.exception("Badge render failed")
        await _record("error", error=f"render failed: {str(e)[:150]}", host=host)
        return {"success": False, "error": str(e)[:150], "host": host}

    # 4. Send to the printer
    result = await print_images(
        images=images,
        host=host,
        model=model,
        label=label_media,
    )

    if result.success:
        await _record("printed", host=host)
        logger.info(f"Badge printed for queue#{item_id} via {host} ({result.bytes_sent} bytes)")
        return {"success": True, "error": None, "host": host}

    await _record("error", error=result.error, host=host)
    return {"success": False, "error": result.error, "host": host}
