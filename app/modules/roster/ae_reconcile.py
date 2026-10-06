"""A/E student reconciliation — Parts A + B.

Consumes the Clever custom-sections sync result to reconcile our
authoritative-status view for A/E-ish students:

Part A — Auto-flag custom-section students as attends_our_classes:
  For every resolved SID in the sync whose membership code has
  enrolled=False, insert into student_attends_our_classes and (if the
  Google account exists) move it into the building's active student OU.
  Rationale: teachers put these kids on a custom-section sheet, so they
  ARE attending the district classes despite the F/R/CTC/etc. code — treat them
  as enrolled and put them where they belong in Google.

Part B — Log-only for other A/E kids:
  Every student with a membership code whose enrolled=False AND no
  attends_our_classes override → candidate for auto-suspend/archive.
  We only LOG these to audit_logs today (action
  roster.ae_reconcile.deprov_candidate) so ops can review and eventually
  flip the code map's auto_deprovision flag with confidence. No Google
  mutation happens in Part B.

Both parts are best-effort — failures on individual students shouldn't
kill the caller. Every action + failure lands in audit_logs so the
history is durable.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def _load_ou_map(db: AsyncSession) -> dict[str, str]:
    """Read the building → student OU map. Same source student
    provisioning uses (roster.student_ou_map JSON)."""
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "roster", "student_ou_map") or "{}"
    try:
        m = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    return m if isinstance(m, dict) else {}


async def flag_custom_section_students(
    db: AsyncSession,
    *,
    resolved_sids_by_building: dict[str, list[str]],
    actor: str = "system:custom_sections_sync",
) -> dict:
    """Part A. `resolved_sids_by_building` = {sis_building_code: [sid, ...]}
    of every SID the sync included in an enrollment CSV whose membership
    code says enrolled=False. For each: upsert attends_our_classes; if
    Google account exists in a non-target OU, move it to the target OU.

    Returns a summary dict for audit logging.
    """
    from app.modules.roster.membership import get_code_map
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    summary = {
        "flagged": 0,          # attends_our_classes rows inserted/updated
        "already_flagged": 0,  # override already present
        "ou_moved": 0,
        "ou_skipped_no_account": 0,
        "ou_skipped_no_map": 0,
        "ou_already_correct": 0,
        "ou_move_failed": 0,
        "errors": [],
    }

    # Flatten to (sid, sis_building) pairs
    pairs: list[tuple[str, str]] = []
    for building, sids in resolved_sids_by_building.items():
        for sid in sids:
            pairs.append((sid, building))
    if not pairs:
        return summary

    # Pull current attends_our_classes membership + Google state.
    all_sids = list({p[0] for p in pairs})
    existing = (await db.execute(text("""
        SELECT sis_id FROM student_attends_our_classes
        WHERE sis_id = ANY(CAST(:sids AS text[]))
    """).bindparams(sids=all_sids))).scalars().all()
    already = set(existing)

    # Roster snapshots for email lookup + building translation
    roster = (await db.execute(text("""
        SELECT sis_id, email, school FROM roster_snapshots
        WHERE sis_id = ANY(CAST(:sids AS text[]))
    """).bindparams(sids=all_sids))).mappings().all()
    email_by_sid = {r["sis_id"]: r["email"] for r in roster if r["email"]}
    internal_by_sid = {r["sis_id"]: r["school"] for r in roster if r["school"]}

    # Building map: sis → internal (SIS bldg codes vary; ou_map uses internal codes)
    from app.modules.settings.buildings import get_building_maps
    bmap = await get_building_maps(db)
    sis_to_internal = bmap.get("sis_to_internal", {})

    ou_map = await _load_ou_map(db)
    gws = None  # lazy-init so a sync with zero flagged rows doesn't touch Google

    for sid, sis_building in pairs:
        # Insert the override (idempotent — reason updated on conflict)
        try:
            await db.execute(text("""
                INSERT INTO student_attends_our_classes (sis_id, reason, added_by, notes)
                VALUES (:sid, :r, :actor, :notes)
                ON CONFLICT (sis_id) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    added_by = EXCLUDED.added_by,
                    added_at = NOW()
            """).bindparams(
                sid=sid,
                r="on custom-section sheet — auto-flagged by sync",
                actor=actor,
                notes=None,
            ))
            if sid in already:
                summary["already_flagged"] += 1
            else:
                summary["flagged"] += 1
        except Exception as e:
            summary["errors"].append(f"attends-flag {sid}: {type(e).__name__}: {str(e)[:80]}")
            continue

        # OU move — only if we have an email and a target OU for the building
        email = email_by_sid.get(sid)
        if not email:
            summary["ou_skipped_no_account"] += 1
            continue

        internal = internal_by_sid.get(sid) or sis_to_internal.get(sis_building)
        target_ou = ou_map.get(internal) if internal else None
        if not target_ou:
            summary["ou_skipped_no_map"] += 1
            continue

        if gws is None:
            gws = GoogleWorkspaceAdapter(db)
        try:
            user = await gws.get_user(email)
            if not user:
                summary["ou_skipped_no_account"] += 1
                continue
            current_ou = (user.get("org_unit_path") or "").rstrip("/")
            if current_ou == target_ou.rstrip("/"):
                summary["ou_already_correct"] += 1
                continue
            result = await gws.move_user_ou(email, target_ou)
            if result.success:
                summary["ou_moved"] += 1
                await db.execute(text("""
                    INSERT INTO audit_logs (actor, action, module, target, details, created_at)
                    VALUES (:a, 'roster.ae_reconcile.ou_moved', 'roster', :t, CAST(:d AS JSONB), NOW())
                """).bindparams(
                    a=actor, t=email,
                    d=json.dumps({"sis_id": sid, "from_ou": current_ou, "to_ou": target_ou}),
                ))
            else:
                summary["ou_move_failed"] += 1
                summary["errors"].append(f"ou-move {email}: {(result.error or '?')[:100]}")
        except Exception as e:
            summary["ou_move_failed"] += 1
            summary["errors"].append(f"ou-move {email}: {type(e).__name__}: {str(e)[:80]}")

    # Truncate errors list so audit blob stays small
    summary["errors"] = summary["errors"][:20]
    return summary


async def log_ae_deprov_candidates(
    db: AsyncSession,
    *,
    actor: str = "system:custom_sections_sync",
) -> dict:
    """Part B. Enumerate students whose membership code says
    enrolled=False AND who don't have an attends_our_classes override,
    filtered to those with an active Google account. Log the list to
    audit_logs as a single roster.ae_reconcile.deprov_candidates event
    so ops can review without any Google mutation happening.

    Returns a small summary dict."""
    from app.modules.roster.membership import get_code_map

    code_map = await get_code_map(db)
    # Codes that are NOT considered enrolled (per code map). Anyone with
    # one of these codes AND no override = candidate.
    not_enrolled_codes = [
        c for c, e in code_map.items() if not e.get("enrolled", False)
    ]
    if not not_enrolled_codes:
        return {"candidates": 0, "by_code": {}}

    rows = (await db.execute(text("""
        SELECT sms.sis_id, sms.code, rs.email, rs.first_name, rs.last_name,
               rs.school, rs.google_status
        FROM student_membership_status sms
        LEFT JOIN roster_snapshots rs ON rs.sis_id = sms.sis_id
        WHERE sms.code = ANY(CAST(:codes AS text[]))
          AND NOT EXISTS (
              SELECT 1 FROM student_attends_our_classes ao WHERE ao.sis_id = sms.sis_id
          )
          AND rs.email IS NOT NULL AND rs.email <> ''
          AND (rs.google_status IS NULL OR rs.google_status = 'active')
        ORDER BY sms.code, rs.school, rs.last_name, rs.first_name
    """).bindparams(codes=not_enrolled_codes))).mappings().all()

    by_code: dict[str, int] = {}
    for r in rows:
        by_code[r["code"]] = by_code.get(r["code"], 0) + 1

    # Sample of candidates — first 25 per code for the audit blob
    sample: list[dict] = []
    seen_per_code: dict[str, int] = {}
    for r in rows:
        c = r["code"]
        if seen_per_code.get(c, 0) >= 25:
            continue
        seen_per_code[c] = seen_per_code.get(c, 0) + 1
        sample.append({
            "sis_id": r["sis_id"],
            "code": r["code"],
            "email": r["email"],
            "school": r["school"],
            "name": f"{r['first_name']} {r['last_name']}",
        })

    await db.execute(text("""
        INSERT INTO audit_logs (actor, action, module, target, details, created_at)
        VALUES (:a, 'roster.ae_reconcile.deprov_candidates', 'roster',
                :t, CAST(:d AS JSONB), NOW())
    """).bindparams(
        a=actor, t="ae_deprov_candidates",
        d=json.dumps({
            "total_candidates": len(rows),
            "by_code": by_code,
            "sample_first_25_per_code": sample,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }),
    ))
    return {"candidates": len(rows), "by_code": by_code}
