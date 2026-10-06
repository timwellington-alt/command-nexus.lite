"""Nightly Clever custom-sections sync.

Orchestrates: read sheet → validate → write-back status → build CSVs →
upload (or dry-run) → audit. See app/modules/roster/clever_custom_sections.py
for the individual steps.

Settings gate (`clever_custom_sections.sync_enabled`) defaults to
dry-run — the job still runs and produces + validates CSVs, but
writes them to docs/clever_custom_out/{date}/ instead of pushing
to Clever. Flip to `true` when you've verified the file contents.

Failure semantics:
- Audit is written in a `finally` block regardless of what happened.
  A silent mid-sync exception used to leave no audit trail and job_health
  marked the run as success. Now: audit ALWAYS lands, capturing whatever
  state we accumulated (tabs_read at minimum, so ops can see "sync
  attempted, saw these tabs, upload failed with X").
- The function RAISES at the end if any errors accumulated (upload
  failure, aborted state, sheet-write errors) — so job_health /
  check_job_liveness see failures instead of masking them as success.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def _write_audit(db, outcome: dict, ctx: dict) -> None:
    """Compose SyncResult from outcome + call audit_sync. Kept small so
    the finally caller doesn't have to know about SyncResult shape."""
    from app.modules.roster.clever_custom_sections import SyncResult, audit_sync
    result = SyncResult(
        dry_run=outcome["dry_run"],
        tabs_read=outcome["tabs_read"],
        sections_built=outcome["sections_built"],
        enrollments_built=outcome["enrollments_built"],
        matched=outcome["matched"],
        ambiguous=outcome["ambiguous"],
        missing_sid=outcome["missing_sid"],
        invalid_teacher=outcome["invalid_teacher"],
        teacher_map_size=outcome["teacher_map_size"],
        custom_teachers_built=outcome["custom_teachers_built"],
        custom_students_built=outcome["custom_students_built"],
        sections_skipped_no_teacher_id=outcome["sections_skipped_no_teacher_id"],
        files_written=outcome["files_written"],
        upload_ok=outcome["upload_ok"],
        upload_details=outcome["upload_details"],
        errors=outcome["errors"],
    )
    actor = ctx.get("actor", "system")
    await audit_sync(db, actor=actor, dry_run=outcome["dry_run"], result=result)
    await db.commit()


async def sync_clever_custom_sections(ctx: dict) -> dict:
    from app.db.engine import AsyncSessionLocal
    from app.modules.roster import clever_custom_sections as ccs
    from app.modules.settings.repository import get_setting_value

    started_at = datetime.now(timezone.utc)
    outcome = {
        "job": "sync_clever_custom_sections",
        "started_at": started_at.isoformat(),
        "aborted": None,
        "dry_run": True,
        "tabs_read": {},
        "sections_built": 0,
        "enrollments_built": 0,
        "custom_teachers_built": 0,
        "custom_students_built": 0,
        "matched": 0,
        "ambiguous": 0,
        "missing_sid": 0,
        "invalid_teacher": 0,
        "teacher_map_size": 0,
        "sections_skipped_no_teacher_id": [],
        "files_written": [],
        "upload_ok": False,
        "upload_details": {},
        "ae_reconcile": {},
        "ae_deprov_candidates": {},
        "errors": [],
    }
    audit_written = False

    try:
        async with AsyncSessionLocal() as db:
            sheet_id = (await get_setting_value(db, "clever_custom_sections", "sheet_id") or "").strip()
            if not sheet_id:
                outcome["aborted"] = "clever_custom_sections.sheet_id not configured"
                return outcome  # audit + raise handled in finally / post

            sync_enabled = (
                await get_setting_value(db, "clever_custom_sections", "sync_enabled")
                or ""
            ).strip().lower() == "true"
            outcome["dry_run"] = not sync_enabled

            try:
                min_rows_floor = int(
                    await get_setting_value(db, "clever_custom_sections", "min_rows_floor")
                    or "1"
                )
            except ValueError:
                min_rows_floor = 1

            # 1. Fetch
            try:
                rows = ccs.read_sheet(sheet_id)
            except Exception as e:
                outcome["aborted"] = f"sheet read failed: {type(e).__name__}: {e}"[:200]
                logger.exception("sheet read failed")
                return outcome

            per_tab_counts: dict[str, int] = {}
            for r in rows:
                per_tab_counts[r.tab] = per_tab_counts.get(r.tab, 0) + 1
            outcome["tabs_read"] = per_tab_counts

            if len(rows) < min_rows_floor:
                outcome["aborted"] = (
                    f"only {len(rows)} row(s) — below min_rows_floor={min_rows_floor}. "
                    f"Would send empty CSVs; refusing (safety gate)."
                )
                return outcome

            # 2. Validate — populates resolved_sid + match_status per row
            await ccs.validate_rows(db, rows)
            for r in rows:
                if r.match_status.startswith("✓"):
                    outcome["matched"] += 1
                elif "ambiguous" in r.match_status:
                    outcome["ambiguous"] += 1
                elif "no student found" in r.match_status or "SID not found" in r.match_status:
                    outcome["missing_sid"] += 1
                if "teacher email not in staff directory" in r.match_status:
                    outcome["invalid_teacher"] += 1

            # 4a. Pull Clever's teachers.csv from SFTP root — email → Teacher_id.
            # Required whether we're going to upload or just dry-run, because
            # both should report accurately which sections would ship.
            host = await get_setting_value(db, "clever_custom_sections", "sftp_host")
            port_raw = await get_setting_value(db, "clever_custom_sections", "sftp_port") or "22"
            username = await get_setting_value(db, "clever_custom_sections", "sftp_username")
            password = await get_setting_value(db, "clever_custom_sections", "sftp_password")
            teacher_map: dict[str, str] = {}
            if host and username and password:
                try:
                    teacher_map = ccs.pull_teacher_id_map(
                        host=host, port=int(port_raw or "22"),
                        username=username, password=password,
                    )
                    outcome["teacher_map_size"] = len(teacher_map)
                except Exception as e:
                    logger.warning(f"teacher_id map pull failed: {e}")
                    outcome["errors"].append(f"teacher_map_pull: {type(e).__name__}: {str(e)[:150]}")

            # 4b. Mint custom Teacher_ids for teachers missing from
            # Clever's teachers.csv but present in staff_directory.
            teacher_map, custom_teachers = await ccs.resolve_missing_teachers(
                db, rows, teacher_map,
            )
            # ALWAYS build customteachers.csv, even when empty (headers only).
            # If we skip the upload when count=0, the OLD customteachers.csv
            # from a prior run stays on Clever's SFTP root and Clever keeps
            # re-hydrating those custom accounts every ingest cycle — that
            # was the "custom account keeps coming back after I deleted it"
            # symptom (2026-09-16). Uploading a fresh empty file every run
            # overwrites the stale one and lets removed teachers stay removed.
            customteachers_csv, customteachers_count = ccs.build_customteachers_csv(custom_teachers)
            outcome["custom_teachers_built"] = customteachers_count

            # 4b'. Lift SID-typed-but-unknown rows into customstudents/
            # so Clever accepts the enrollment. Same always-upload
            # contract as customteachers — empty file overwrites the
            # stale copy from a prior run so obsolete custom students
            # get de-registered.
            custom_students = ccs.resolve_missing_students(rows)
            customstudents_csv, customstudents_count = ccs.build_customstudents_csv(custom_students)
            outcome["custom_students_built"] = customstudents_count

            # Re-tally match categories after resolve_missing_students —
            # rows we just lifted moved out of missing_sid into matched.
            outcome["matched"] = 0
            outcome["ambiguous"] = 0
            outcome["missing_sid"] = 0
            outcome["invalid_teacher"] = 0
            for r in rows:
                if r.match_status.startswith("✓"):
                    outcome["matched"] += 1
                elif "ambiguous" in r.match_status:
                    outcome["ambiguous"] += 1
                elif "no student found" in r.match_status or "SID not found" in r.match_status:
                    outcome["missing_sid"] += 1
                if "teacher email not in staff directory" in r.match_status:
                    outcome["invalid_teacher"] += 1

            # 4c. Build CSVs
            sections_csv, sections_count, skipped_no_tid = ccs.build_sections_csv(rows, teacher_map)
            enrollments_csv, enrollments_count = ccs.build_enrollments_csv(rows, teacher_map)
            outcome["sections_built"] = sections_count
            outcome["enrollments_built"] = enrollments_count
            outcome["sections_skipped_no_teacher_id"] = sorted(set(skipped_no_tid))

            # Write Match Status back to the sheet — after build so
            # teacher-not-in-Clever statuses land in the same sync.
            try:
                ccs.write_match_status(sheet_id, rows)
            except Exception as e:
                logger.warning(f"Match-status write-back failed: {e}")
                outcome["errors"].append(f"sheet_writeback: {str(e)[:120]}")

            # Dashboard tab — mismatched rows + custom students without
            # an email. Written after write_match_status so the status
            # column reflects the latest run when we filter on it.
            try:
                ccs.write_dashboard(sheet_id, rows)
            except Exception as e:
                logger.warning(f"Dashboard write failed: {e}")
                outcome["errors"].append(f"dashboard_write: {str(e)[:120]}")

            # 5. Deliver
            if outcome["dry_run"]:
                paths = ccs.write_dry_run(
                    sections_csv, enrollments_csv,
                    customteachers_csv,   # always include even if empty
                    customstudents_csv,   # always include even if empty
                )
                outcome["files_written"] = paths
                logger.info(
                    f"Dry-run: wrote {len(paths)} file(s) to "
                    f"docs/clever_custom_out — set clever_custom_sections."
                    f"sync_enabled=true to enable SFTP push"
                )
            else:
                if not (host and username and password):
                    outcome["aborted"] = "SFTP host/username/password missing — cannot upload"
                    return outcome
                try:
                    port = int(port_raw)
                except ValueError:
                    port = 22
                try:
                    all_hashes: dict[str, str] = {}
                    # 1) ALWAYS push customteachers, even empty (headers only).
                    # Purpose is idempotent: the file on Clever's SFTP mirrors
                    # our current custom-teacher list. When a teacher stops
                    # needing a custom entry (added to native teachers.csv,
                    # or manually removed by an operator), overwriting with
                    # an empty file prevents Clever from re-hydrating the
                    # obsolete custom account. See resolve_missing_teachers
                    # for how the list is composed.
                    ct_hashes = ccs.upload_sftp(
                        host=host, port=port,
                        username=username, password=password,
                        remote_path="customteachers",
                        files={"teachers.csv": customteachers_csv},
                    )
                    for k, v in ct_hashes.items():
                        all_hashes[f"customteachers/{k}"] = v
                    # 1b) ALWAYS push customstudents (same rationale) —
                    # the enrollments Clever accepts against these SIDs
                    # rely on the students.csv shipping alongside them.
                    cs_hashes = ccs.upload_sftp(
                        host=host, port=port,
                        username=username, password=password,
                        remote_path="customstudents",
                        files={"students.csv": customstudents_csv},
                    )
                    for k, v in cs_hashes.items():
                        all_hashes[f"customstudents/{k}"] = v
                    # 2) Then customsections
                    remote_path = (
                        await get_setting_value(db, "clever_custom_sections", "sftp_remote_path")
                        or "customsections"
                    )
                    sec_hashes = ccs.upload_sftp(
                        host=host, port=port,
                        username=username, password=password,
                        remote_path=remote_path,
                        files={"sections.csv": sections_csv, "enrollments.csv": enrollments_csv},
                    )
                    for k, v in sec_hashes.items():
                        all_hashes[f"customsections/{k}"] = v
                    outcome["upload_ok"] = True
                    outcome["upload_details"] = all_hashes
                    logger.info(
                        f"SFTP upload complete → {host}:{port}{remote_path} — "
                        f"{sections_count} sections, {enrollments_count} enrollments"
                    )
                except Exception as e:
                    logger.exception("SFTP upload failed")
                    outcome["errors"].append(f"sftp_upload: {type(e).__name__}: {str(e)[:200]}")

            # 5b. A/E reconciliation — only runs on successful upload,
            # so a sync that failed to reach Clever doesn't nonetheless
            # move Google OUs based on stale data.
            if outcome["upload_ok"]:
                try:
                    from app.modules.roster.ae_reconcile import (
                        flag_custom_section_students, log_ae_deprov_candidates,
                    )
                    from app.modules.roster.membership import get_code_map
                    from sqlalchemy import text as _sa_text
                    # Group resolved SIDs by their SIS building code, but
                    # only for rows whose membership code says enrolled=False
                    # (the whole point of the auto-flag is to lift A/E-ish
                    # kids into effectively-enrolled state).
                    code_map = await get_code_map(db)
                    ms_rows = (await db.execute(_sa_text("""
                        SELECT sis_id, code FROM student_membership_status
                        WHERE sis_id = ANY(CAST(:sids AS text[]))
                    """).bindparams(sids=[r.resolved_sid for r in rows if r.resolved_sid]))).mappings().all()
                    code_by_sid = {r["sis_id"]: r["code"] for r in ms_rows}
                    to_flag: dict[str, list[str]] = {}
                    for r in rows:
                        if not r.resolved_sid or not r.building_sis:
                            continue
                        code = code_by_sid.get(r.resolved_sid)
                        if not code:
                            continue
                        if code_map.get(code, {}).get("enrolled", False):
                            continue  # already enrolled per code — no flip needed
                        to_flag.setdefault(r.building_sis, []).append(r.resolved_sid)
                    part_a = await flag_custom_section_students(
                        db, resolved_sids_by_building=to_flag,
                        actor="system:custom_sections_sync",
                    )
                    outcome["ae_reconcile"] = part_a

                    # Part B — log-only enumeration of A/E kids who
                    # aren't on any custom sheet AND don't have an
                    # override. Written directly into audit_logs; the
                    # summary lands in outcome for the sync report too.
                    part_b = await log_ae_deprov_candidates(
                        db, actor="system:custom_sections_sync",
                    )
                    outcome["ae_deprov_candidates"] = part_b
                    logger.info(
                        "A/E reconcile: flagged=%d ou_moved=%d | deprov_candidates=%d",
                        part_a["flagged"], part_a["ou_moved"], part_b["candidates"],
                    )
                except Exception as e:
                    logger.exception("A/E reconciliation failed (sync itself is OK)")
                    outcome["errors"].append(f"ae_reconcile: {type(e).__name__}: {str(e)[:200]}")

            # 6. Audit inside the main session (fast path — same db)
            await _write_audit(db, outcome, ctx)
            audit_written = True

    except Exception as e:
        logger.exception("sync_clever_custom_sections failed")
        outcome["errors"].append(f"{type(e).__name__}: {str(e)[:200]}")

    finally:
        # If the main session died before we could audit, open a fresh
        # one and write the audit anyway — even a partial record
        # (tabs_read up to the crash + the error string) is a huge
        # improvement over silent-failure.
        if not audit_written:
            try:
                async with AsyncSessionLocal() as db2:
                    await _write_audit(db2, outcome, ctx)
            except Exception as ae:
                logger.warning(f"fallback audit write failed: {ae}")
                outcome["errors"].append(f"audit_write_failed: {str(ae)[:150]}")

    # Bubble up failure so job_health / check_liveness see it as failed
    # instead of masking it as success. Silent aborts hid a real config
    # or SFTP issue for days (2026-09-15 Wolfenbarker case).
    fatal = bool(outcome.get("aborted") or outcome["errors"])
    if not outcome["dry_run"] and not outcome["upload_ok"] and outcome.get("aborted") is None and not outcome["errors"]:
        # Sync ran in enabled mode but no upload happened AND no error
        # captured — should be impossible given the paths above, but
        # covers a future path that forgets to set either.
        outcome["errors"].append("no upload attempted (unknown reason)")
        fatal = True

    if fatal:
        raise RuntimeError(
            "sync_clever_custom_sections failed — "
            f"aborted={outcome.get('aborted')!r}, "
            f"errors={outcome['errors'][:3]}"
        )
    return outcome
