"""
Clever roster import via Gmail — polls a mailbox for CSV attachments.

Clever sends automated report emails to a designated Gmail address.
This job impersonates that mailbox via the service account, searches
for unread emails matching configured subject patterns, downloads CSV
attachments, and feeds them into the roster import pipeline.

Required Google API scopes on the service account:
- https://www.googleapis.com/auth/gmail.readonly
- https://www.googleapis.com/auth/gmail.modify (to mark as read)
"""

import base64
import csv
import io
import json as _json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)


async def poll_clever_imports(ctx: dict) -> dict:
    """
    Poll Gmail for Clever CSV reports and import them.

    Returns {"processed": N, "emails_found": M, ...}.
    """
    from app.db.engine import AsyncSessionLocal
    import asyncio

    result = {
        "job": "poll_clever_imports",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "emails_found": 0,
        "processed": 0,
        "errors": [],
    }

    try:
        async with AsyncSessionLocal() as db:
            from app.modules.settings.repository import get_setting_value

            # NOTE: previously this poll had a 60-min window gate anchored
            # on roster.import_time — scheduled runs outside that window
            # returned early. The gate worked when the poll ONLY handled
            # the morning Clever export (Students/Teachers/etc. arriving
            # ~02:30). Once Daily Attendance ingestion was added to the
            # same job, the gate silently killed mid-morning attendance
            # pulls (MetaSolutions emails their attendance report ~10:30
            # local, well outside the 2:30 window). Two under-count
            # incidents (2026-09-10 and 2026-09-11) traced to this.
            #
            # Fix: remove the gate. The 15-min scheduler cadence + the
            # clever_processed dedup table are the real cost controls —
            # polls without a new message are ~50ms Gmail round-trips
            # and cost effectively nothing.
            is_scheduled = ctx.get("job_id", "").startswith("scheduled:")

            gmail_user = await get_setting_value(db, "google", "clever_gmail_user")
            admin_email = await get_setting_value(db, "google", "admin_email")
            subjects_raw = await get_setting_value(db, "roster", "clever_subjects")

            if not gmail_user:
                result["errors"].append("clever_gmail_user not configured")
                return result
            if not subjects_raw:
                result["errors"].append("clever_subjects not configured")
                return result

            subjects = [s.strip() for s in subjects_raw.split(",") if s.strip()]
            if not subjects:
                result["errors"].append("No subject patterns configured")
                return result

            # Build Gmail service
            cred_file = os.environ.get(
                "GOOGLE_SERVICE_ACCOUNT_FILE",
                "/run/secrets/google_service_account.json",
            )

            def _build_gmail():
                from google.oauth2 import service_account
                from googleapiclient.discovery import build
                creds = (
                    service_account.Credentials
                    .from_service_account_file(
                        cred_file,
                        scopes=[
                            "https://www.googleapis.com/auth/gmail.readonly",
                            "https://www.googleapis.com/auth/gmail.modify",
                        ],
                    )
                    .with_subject(gmail_user)
                )
                return build("gmail", "v1", credentials=creds, cache_discovery=False)

            gmail = await asyncio.to_thread(_build_gmail)

            # Collect teacher and enrollment data across all emails
            # so we can cross-reference them at the end (v1 pattern)
            collected_teachers = {}   # teacher_id → {First_name, Last_name, Teacher_email, ...}
            collected_enrollments = []  # [{school_id, section_id, student_id}, ...]
            collected_section_meta = {}  # section_id → {period, course_name, subject}
            # Union of every sis_id seen in every Students CSV processed
            # this run. Populated by _process_csv returns. Drives the
            # single district-wide withdrawal diff below.
            all_sis_ids_seen: set[str] = set()
            withdrawn_students = []     # Populated by the district-wide diff, not per-CSV
            all_roster_changes = []     # diff changes from all student CSV imports
            # True iff at least one Students CSV successfully processed
            # this run. Used to short-circuit the withdrawal diff — running
            # it when zero Students CSVs landed would (correctly) treat
            # every roster row as withdrawn, which is catastrophic. Only
            # diff when we have at least one authoritative snapshot.
            students_csv_processed = False
            # True iff at least one attendance CSV (Daily Attendance / Absence
            # List) was ingested this run. Triggers a chained
            # snapshot_attendance_analytics enqueue so the /roster/analytics
            # tables + downstream emails see today's numbers on the next tick.
            absences_ingested = False

            # Load already-processed message IDs
            from sqlalchemy import text as sa_text
            processed_rows = await db.execute(sa_text("SELECT msg_id FROM clever_processed"))
            already_processed = {r[0] for r in processed_rows.all()}

            # ── New format (MetaSolutions single-email, multi-CSV) ──
            # One message from ReportingServices@metasolutions.net carries
            # all six data types as separate attachments named
            # Students.csv / Teachers.csv / Enrollments.csv / Sections.csv
            # / Staff.csv / Schools.csv. We classify each attachment by
            # filename (not subject) so a rename of the email doesn't
            # break the pipeline. Enabled via settings so operators can
            # roll it out per-district; falls through to the legacy
            # subject loop when no matching sender is configured.
            meta_sender = (await get_setting_value(
                db, "roster", "clever_metasolutions_sender"
            ) or "").strip()
            meta_subject = (await get_setting_value(
                db, "roster", "clever_metasolutions_subject"
            ) or "").strip()
            if meta_sender:
                try:
                    meta_emails = await _find_unread_metasolutions_emails(
                        gmail, meta_sender, meta_subject,
                    )
                    result["emails_found"] += len(meta_emails)
                    for msg_id in meta_emails:
                        if msg_id in already_processed:
                            continue
                        try:
                            attachments = await _download_all_csv_attachments(gmail, msg_id)
                            if not attachments:
                                result["errors"].append(
                                    f"MetaSolutions email {msg_id}: no CSV attachments"
                                )
                                continue
                            for fname, csv_data in attachments:
                                import_type = _classify_filename(fname)
                                if not import_type:
                                    result["errors"].append(
                                        f"MetaSolutions email {msg_id}: unrecognized attachment {fname!r}"
                                    )
                                    continue
                                teachers, enrollments, section_meta, csv_sis_ids, csv_changes = await _process_csv(
                                    db, csv_data, import_type,
                                    f"MetaSolutions:{fname}",
                                )
                                if import_type == "attendance":
                                    absences_ingested = True
                                if teachers:
                                    collected_teachers.update(teachers)
                                if enrollments:
                                    collected_enrollments.extend(enrollments)
                                if section_meta:
                                    collected_section_meta.update(section_meta)
                                # csv_sis_ids is only populated by Students
                                # CSVs — teachers/enrollments/etc. return
                                # None or empty. Union means processing
                                # multiple Students CSVs (backlog) unions
                                # their coverage rather than colliding.
                                if csv_sis_ids:
                                    all_sis_ids_seen |= set(csv_sis_ids)
                                    students_csv_processed = True
                                if csv_changes:
                                    all_roster_changes.extend(csv_changes)
                            await db.execute(sa_text(
                                "INSERT INTO clever_processed (msg_id, subject) VALUES (:mid, :subj) ON CONFLICT DO NOTHING"
                            ).bindparams(mid=msg_id, subj=f"MetaSolutions:{meta_subject or '(any)'}"))
                            await _mark_as_read(gmail, msg_id)
                            result["processed"] += 1
                            logger.info(
                                f"MetaSolutions email {msg_id}: processed "
                                f"{len(attachments)} CSV(s)"
                            )
                        except Exception as e:
                            result["errors"].append(f"MetaSolutions {msg_id}: {str(e)[:150]}")
                except Exception as e:
                    result["errors"].append(f"MetaSolutions sender scan: {str(e)[:150]}")

            # Sender allowlist for the legacy subject loop. When set, every
            # subject search is scoped to `from:{meta_sender}`, blocking
            # look-alike emails from a shadow/legacy sender that share the
            # same subject (see reference_metasolutions_source_of_truth
            # memory + Aug 2026 ProgressBook Ad Hoc leak).
            for subject in subjects:
                try:
                    emails = await _find_unread_emails(gmail, subject, sender=meta_sender or None)
                    result["emails_found"] += len(emails)

                    for msg_id in emails:
                        if msg_id in already_processed:
                            continue  # Skip already-processed emails
                        try:
                            csv_data = await _download_csv_attachment(gmail, msg_id)
                            if csv_data:
                                import_type = _classify_subject(subject)
                                teachers, enrollments, section_meta, csv_sis_ids, csv_changes = await _process_csv(
                                    db, csv_data, import_type, subject,
                                )
                                if import_type == "attendance":
                                    absences_ingested = True
                                if teachers:
                                    collected_teachers.update(teachers)
                                if enrollments:
                                    collected_enrollments.extend(enrollments)
                                if section_meta:
                                    collected_section_meta.update(section_meta)
                                if csv_sis_ids:
                                    all_sis_ids_seen |= set(csv_sis_ids)
                                    students_csv_processed = True
                                if csv_changes:
                                    all_roster_changes.extend(csv_changes)
                                # Record as processed
                                await db.execute(sa_text(
                                    "INSERT INTO clever_processed (msg_id, subject) VALUES (:mid, :subj) ON CONFLICT DO NOTHING"
                                ).bindparams(mid=msg_id, subj=subject))
                                await _mark_as_read(gmail, msg_id)
                                result["processed"] += 1
                            else:
                                result["errors"].append(f"No CSV attachment in email {msg_id}")
                        except Exception as e:
                            result["errors"].append(f"Email {msg_id}: {str(e)[:100]}")

                except Exception as e:
                    result["errors"].append(f"Subject '{subject}': {str(e)[:100]}")

            # Cross-reference enrollments with teachers to build student→teacher mapping
            if collected_enrollments and collected_teachers:
                await _build_student_teacher_map(db, collected_enrollments, collected_teachers, collected_section_meta)
                result["student_teacher_pairs"] = len(collected_enrollments)
            elif collected_enrollments and not collected_teachers:
                logger.warning("Enrollment data collected but no teacher data — student-teacher mapping not updated")

            # Enrich student_teachers with period + course from sections CSV
            # Must happen AFTER _build_student_teacher_map which rebuilds the table
            if collected_section_meta:
                await _enrich_sections(db, collected_section_meta)

            # ── Provisioning pipeline (new students) ─────────────────────
            if all_roster_changes:
                try:
                    from app.modules.roster.provision import provision_new_students, handle_transfers, handle_grade_changes
                    prov_result = await provision_new_students(db, all_roster_changes)
                    result["provisioned"] = prov_result.get("provisioned", 0)
                    result["reactivated"] = prov_result.get("reactivated", 0)
                    result["review_flagged"] = prov_result.get("review_flagged", 0)
                    if prov_result.get("skipped_reason"):
                        result["provision_skipped"] = prov_result["skipped_reason"]

                    # Persist per-student outcomes — 'errors=0' on the
                    # import row hides silent skips, so we need durable
                    # per-student trail to answer "what did Nexus try
                    # to do for student X on run Y" (see 2026-08-20
                    # Briggs/Hersey vanishing-account case).
                    try:
                        added_count = sum(
                            1 for c in all_roster_changes
                            if c.get("change_type") == "added"
                        )
                        await db.execute(sa_text("""
                            INSERT INTO roster_provisioning_runs
                                (source, actor, provisioned, reactivated,
                                 review_flagged, skipped, errors,
                                 skipped_reason, candidate_count, details)
                            VALUES (:source, :actor, :prov, :react, :rev,
                                    :skip, :err, :sr, :cand,
                                    CAST(:det AS JSONB))
                        """).bindparams(
                            source="clever_import",
                            actor="system",
                            prov=int(prov_result.get("provisioned", 0)),
                            react=int(prov_result.get("reactivated", 0)),
                            rev=int(prov_result.get("review_flagged", 0)),
                            skip=int(prov_result.get("skipped", 0)),
                            err=int(prov_result.get("errors", 0)),
                            sr=prov_result.get("skipped_reason"),
                            cand=added_count,
                            det=_json.dumps(prov_result.get("details", [])),
                        ))
                    except Exception as e:
                        logger.warning(f"Could not persist prov_result: {e}")

                    # Handle transfers — OU moves + guidance queue
                    transfer_result = await handle_transfers(db, all_roster_changes)
                    result["transfers_moved"] = transfer_result.get("moved", 0)
                    result["transfers_guidance_queued"] = transfer_result.get("guidance_queued", 0)

                    # Handle grade changes — RENAME existing account when
                    # the new grad year shifts the expected email suffix
                    # (retentions or advancements). Never mints a fresh
                    # account for the same student. Prevents the orphan-
                    # duplicate pattern that produced ~114 stale accounts.
                    gc_result = await handle_grade_changes(db, all_roster_changes)
                    result["grade_change_renamed"] = gc_result.get("renamed", 0)
                    result["grade_change_already_correct"] = gc_result.get("already_correct", 0)
                except Exception as e:
                    logger.warning(f"Provisioning pipeline failed: {e}")
                    result["errors"].append(f"Provisioning: {str(e)[:100]}")

                # Log change summary
                change_counts = {}
                for c in all_roster_changes:
                    ct = c.get("change_type", "unknown")
                    change_counts[ct] = change_counts.get(ct, 0) + 1
                result["roster_changes"] = change_counts

            # Auto-queue new enrollments without sections for guidance counselors
            # and auto-resolve any pending entries that now have sections
            try:
                from app.modules.roster.guidance_auto import auto_queue_new_enrollments, auto_resolve_with_sections
                guidance_result = await auto_queue_new_enrollments(db)
                result["guidance_queued"] = guidance_result.get("queued", 0)
                resolved = await auto_resolve_with_sections(db)
                result["guidance_resolved"] = resolved
            except Exception as e:
                logger.warning(f"Guidance auto-queue failed: {e}")

            # ── District-wide withdrawal diff (replaces per-CSV logic) ──
            #
            # The old per-CSV mark_students_inactive path fired false
            # withdrawals every time a run processed more than one
            # Students CSV (backlog, multi-day catch-up). See the
            # 2026-08-27 incident notes. New logic:
            #
            #   1. Only run when at least one Students CSV was seen.
            #      Zero Students CSVs = don't diff — otherwise every
            #      row in the roster would look withdrawn.
            #   2. Compare union of sis_ids across all Students CSVs
            #      against currently-active roster_snapshots.
            #   3. Sanity gate — if the withdrawn count exceeds
            #      roster.withdraw_cascade_floor (default 100) OR 5% of
            #      the district's active count, ABORT the deprovision.
            #      Log an audit row + skip the guidance queue.
            #      Operator can raise the floor + rerun manually if
            #      it's a legitimate mass event (year-end, etc.).
            #   4. Otherwise: mark inactive + queue for deprovision.
            withdrawal_aborted_reason: str | None = None
            if students_csv_processed:
                total_active = (await db.execute(sa_text(
                    "SELECT count(*) FROM roster_snapshots WHERE status='active'"
                ))).scalar_one() or 0
                withdrawn_rows = (await db.execute(sa_text("""
                    SELECT id, sis_id, first_name, last_name, email, school, grade
                    FROM roster_snapshots
                    WHERE status = 'active' AND sis_id != ALL(:seen)
                """).bindparams(seen=list(all_sis_ids_seen)))).mappings().all()
                withdrawn_count = len(withdrawn_rows)

                # Directory-diff sanity gate. See feedback_dir_diff_sanity_gate
                # and the 2026-08-27 partial-CSV incident.
                floor = int(await get_setting_value(
                    db, "roster", "withdraw_cascade_floor"
                ) or "100")
                pct_cap = float(await get_setting_value(
                    db, "roster", "withdraw_cascade_max_pct"
                ) or "5") / 100.0
                pct_ceiling = int(total_active * pct_cap) if total_active else 0

                if withdrawn_count > floor and withdrawn_count > pct_ceiling:
                    withdrawal_aborted_reason = (
                        f"withdrawn count {withdrawn_count} exceeds both "
                        f"floor ({floor}) and {int(pct_cap*100)}% cap "
                        f"({pct_ceiling}) of {total_active} active. "
                        "Refusing to cascade — probably a partial CSV. "
                        "Raise roster.withdraw_cascade_floor if this is "
                        "a legitimate mass withdrawal (year-end)."
                    )
                    logger.error(
                        f"Withdrawal cascade GATED: {withdrawal_aborted_reason}"
                    )
                    from app.audit.service import log_action
                    await log_action(
                        db, actor="system",
                        action="roster.import.withdraw_cascade_gated",
                        module="roster",
                        target=f"{withdrawn_count} would-be withdrawals",
                        details=_json.dumps({
                            "reason": withdrawal_aborted_reason,
                            "withdrawn_count": withdrawn_count,
                            "total_active": total_active,
                            "floor": floor,
                            "pct_cap": pct_cap,
                            "seen_sis_ids": len(all_sis_ids_seen),
                            "sample_withdrawn": [
                                {"sis_id": r["sis_id"],
                                 "name": f"{r['first_name']} {r['last_name']}",
                                 "school": r["school"]}
                                for r in list(withdrawn_rows)[:10]
                            ],
                        }),
                    )
                    result["withdraw_cascade_gated"] = True
                    result["withdraw_cascade_reason"] = withdrawal_aborted_reason
                    result["would_be_withdrawn"] = withdrawn_count
                else:
                    # Legitimate withdrawals — mark them inactive and
                    # populate the deprovision list.
                    for r in withdrawn_rows:
                        await db.execute(sa_text(
                            "UPDATE roster_snapshots SET status='inactive' "
                            "WHERE id = :id"
                        ).bindparams(id=r["id"]))
                        withdrawn_students.append({
                            "id": r["id"],
                            "sis_id": r["sis_id"],
                            "first_name": r["first_name"],
                            "last_name": r["last_name"],
                            "email": r["email"],
                            "school": r["school"],
                            "grade": r["grade"],
                        })
                    if withdrawn_count:
                        logger.info(
                            f"District-wide withdrawal diff: "
                            f"{withdrawn_count} students (floor={floor}, "
                            f"cap={pct_ceiling})"
                        )
            else:
                logger.info(
                    "Withdrawal diff skipped — no Students CSV processed "
                    "this run (safe default: never assume the whole roster "
                    "left)."
                )

            # Queue withdrawals for guidance counselors
            if withdrawn_students:
                try:
                    from app.modules.roster.guidance_auto import auto_queue_withdrawals
                    wq_result = await auto_queue_withdrawals(db, withdrawn_students)
                    result["withdrawal_queued"] = wq_result.get("queued", 0)
                except Exception as e:
                    logger.warning(f"Withdrawal auto-queue failed: {e}")

            # Deprovision withdrawn students — move to archive OU + suspend
            # Only runs when student_google_writes_enabled is true
            if withdrawn_students:
                result["withdrawn"] = len(withdrawn_students)
                try:
                    from app.modules.settings.repository import get_setting_value as _gw
                    from app.integrations.api_log import log_api_command as _log_api
                    writes_enabled = await _gw(db, "roster", "student_google_writes_enabled")
                    deprov_ou = await _gw(db, "roster", "student_deprovision_ou")

                    if writes_enabled and writes_enabled.lower() == "true" and deprov_ou:
                        from app.integrations.google.adapter import GoogleWorkspaceAdapter
                        gws = GoogleWorkspaceAdapter(db)
                        deprov_count = 0
                        deprov_errors = []

                        for student in withdrawn_students:
                            email = (student.get("email") or "").strip()
                            if not email:
                                continue
                            try:
                                # Move to archive OU
                                move_r = await gws.move_user_ou(email, deprov_ou)
                                _log_api(
                                    system="google", action="move_user_ou", target=email,
                                    payload=f"deprov_ou={deprov_ou}",
                                    response_status="ok" if move_r.success else "error",
                                    caller="clever_import_job.deprovision",
                                )
                                # Suspend the account
                                susp_r = await gws.suspend_user(email)
                                _log_api(
                                    system="google", action="suspend_user", target=email,
                                    response_status="ok" if susp_r.success else "error",
                                    caller="clever_import_job.deprovision",
                                )
                                # Revoke Ed Plus license (paid, ~$5/seat).
                                # Idempotent — no-op if the user doesn't hold
                                # it. Added 2026-09-01 after the manual
                                # 1,146-seat reclaim showed archived + inactive
                                # students had been accumulating paid seats
                                # for years. Product 101031, SKU 1010310008
                                # = current Ed Plus (NOT the Legacy 1010310002).
                                # Requires apps.licensing scope on the SA's DWD.
                                lic_r = await gws.remove_license(
                                    email, product_id="101031", sku_id="1010310008",
                                )
                                _log_api(
                                    system="google", action="remove_license",
                                    target=email, payload="sku=1010310008",
                                    response_status="ok" if lic_r.success else "error",
                                    caller="clever_import_job.deprovision",
                                )
                                deprov_count += 1
                                logger.info(f"Deprovisioned withdrawn student: {student['first_name'][:1]}. {student['last_name']} ({email}) → {deprov_ou}")
                            except Exception as e:
                                deprov_errors.append(f"{email}: {str(e)[:100]}")
                                logger.warning(f"Deprovision failed for {email}: {e}")

                        result["deprovisioned"] = deprov_count
                        if deprov_errors:
                            result["deprovision_errors"] = deprov_errors

                        if deprov_count:
                            from app.audit.service import log_action
                            await log_action(
                                db, actor="system", action="student_account:deprovision.auto",
                                module="roster",
                                target=f"{deprov_count} students deprovisioned",
                                details=f"Moved to {deprov_ou}, suspended, Ed Plus (1010310008) revoked",
                            )
                    else:
                        result["deprovision_skipped"] = "Google writes disabled or no archive OU configured"
                        logger.info(f"Withdrawn students: {len(withdrawn_students)} — deprovision skipped (writes_enabled={writes_enabled}, deprov_ou={deprov_ou})")
                except Exception as e:
                    logger.warning(f"Deprovision pipeline failed: {e}")

            await db.commit()

    except Exception as e:
        logger.error(f"poll_clever_imports failed: {e}")
        result["errors"].append(str(e)[:200])
        raise

    # Chain: when we ingested attendance data, refresh the analytics
    # tables so /roster/analytics + the daily digest email see today's
    # numbers on the next tick instead of yesterday's snapshot. Use
    # the arq redis pool from ctx (present on real worker runs; absent
    # on manual invocations, which we skip cleanly).
    if absences_ingested:
        result["chained_snapshot_enqueued"] = False
        try:
            redis = ctx.get("redis")
            if redis is not None:
                await redis.enqueue_job(
                    "snapshot_attendance_analytics",
                    _job_id=f"chained:snapshot_attendance_analytics:{int(datetime.now(timezone.utc).timestamp())}",
                )
                result["chained_snapshot_enqueued"] = True
                logger.info("Chained snapshot_attendance_analytics enqueued after attendance ingest")
        except Exception as e:
            logger.warning(f"Failed to enqueue chained snapshot_attendance_analytics: {e}")
            result["errors"].append(f"chained_enqueue: {str(e)[:100]}")

    logger.info(
        f"Clever import: {result['emails_found']} emails found, "
        f"{result['processed']} processed, {len(result['errors'])} errors"
    )
    return result


async def _find_unread_emails(
    gmail, subject: str, sender: str | None = None,
) -> list[str]:
    """Find unread emails matching a subject pattern — today only.

    When ``sender`` is set, the search is constrained to that From: address
    so subject look-alikes from other senders (e.g. legacy report streams)
    don't leak in.
    """
    import asyncio
    from zoneinfo import ZoneInfo

    tz_name = "America/New_York"
    try:
        from app.db.engine import AsyncSessionLocal
        from app.modules.settings.repository import get_setting_value as _gsv
        async with AsyncSessionLocal() as _db:
            _tz = await _gsv(_db, "branding", "timezone")
            if _tz:
                tz_name = _tz
    except Exception:
        pass

    def _search():
        local_tz = ZoneInfo(tz_name)
        now_local = datetime.now(local_tz)
        today_midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_midnight_utc = today_midnight_local.astimezone(timezone.utc)
        epoch = int(today_midnight_utc.timestamp())

        safe_subject = subject.replace('"', '\\"')
        parts = [f'subject:"{safe_subject}"', 'has:attachment', f'after:{epoch}']
        if sender:
            parts.insert(0, f'from:{sender}')
        query = " ".join(parts)
        results = gmail.users().messages().list(
            userId="me", q=query, maxResults=10,
        ).execute()
        return [m["id"] for m in results.get("messages", [])]

    return await asyncio.to_thread(_search)


async def _find_unread_metasolutions_emails(
    gmail, sender: str, subject: str | None,
) -> list[str]:
    """Find today's unread emails from the MetaSolutions ReportingServices
    sender (Clever exports). Subject is included in the query when
    configured — useful to narrow further if the same sender emits
    other reports the operator doesn't want processed."""
    import asyncio
    from zoneinfo import ZoneInfo

    tz_name = "America/New_York"
    try:
        from app.db.engine import AsyncSessionLocal
        from app.modules.settings.repository import get_setting_value as _gsv
        async with AsyncSessionLocal() as _db:
            _tz = await _gsv(_db, "branding", "timezone")
            if _tz:
                tz_name = _tz
    except Exception:
        pass

    def _search():
        local_tz = ZoneInfo(tz_name)
        now_local = datetime.now(local_tz)
        today_midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_midnight_utc = today_midnight_local.astimezone(timezone.utc)
        epoch = int(today_midnight_utc.timestamp())

        parts = [f'from:{sender}', 'has:attachment', f'after:{epoch}']
        if subject:
            safe = subject.replace('"', '\\"')
            parts.append(f'subject:"{safe}"')
        query = " ".join(parts)
        results = gmail.users().messages().list(
            userId="me", q=query, maxResults=10,
        ).execute()
        return [m["id"] for m in results.get("messages", [])]

    return await asyncio.to_thread(_search)


async def _download_all_csv_attachments(gmail, msg_id: str) -> list[tuple[str, str]]:
    """Return every CSV attachment as (filename, decoded_text). Handles
    multipart/mixed with N .csv attachments — the MetaSolutions email
    ships all six datasets in one message. Walks nested parts because
    Gmail sometimes wraps attachments in a multipart/alternative
    inside the top-level multipart/mixed."""
    import asyncio

    def _walk(parts, out):
        for part in parts or []:
            filename = part.get("filename", "")
            if filename.lower().endswith(".csv"):
                attach_id = part.get("body", {}).get("attachmentId")
                if attach_id:
                    out.append((filename, attach_id))
            _walk(part.get("parts"), out)

    def _download():
        msg = gmail.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()
        pending: list[tuple[str, str]] = []
        _walk(msg.get("payload", {}).get("parts"), pending)
        results: list[tuple[str, str]] = []
        for filename, attach_id in pending:
            attach = gmail.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=attach_id,
            ).execute()
            try:
                text = base64.urlsafe_b64decode(attach["data"]).decode("utf-8-sig")
            except Exception:
                continue
            results.append((filename, text))
        return results

    return await asyncio.to_thread(_download)


def _classify_filename(filename: str) -> str | None:
    """Map an attachment filename to a Clever import type. Case- and
    hyphen-insensitive. Returns None when the filename doesn't match
    any recognized type — caller logs it so nothing gets silently
    dropped."""
    stem = (filename or "").rsplit(".", 1)[0].lower().replace("-", "").replace("_", "").replace(" ", "")
    if "absencelist" in stem or "attendance" in stem:
        return "attendance"
    if "student" in stem:
        return "students"
    if "teacher" in stem:
        return "teachers"
    if "enrollment" in stem:
        return "enrollments"
    if "section" in stem:
        return "sections"
    if "admin" in stem or "staff" in stem:
        return "admins"
    if "school" in stem:
        # Schools CSV isn't currently consumed by _process_csv — skip
        # cleanly so we don't misclassify it as "students".
        return None
    return None


async def _download_csv_attachment(gmail, msg_id: str) -> str | None:
    """Download the first CSV attachment from an email.

    Walks nested parts because MetaSolutions Clever exports nest the
    attachment under multipart/alternative → multipart/mixed instead
    of the single-level structure Clever's original vendor used. The
    old single-level scan silently dropped every MetaSolutions message
    with a 'No CSV attachment' error even though the .csv was there."""
    import asyncio

    def _walk(parts, found):
        for p in parts or []:
            fn = p.get("filename", "")
            if fn.lower().endswith(".csv") and p.get("body", {}).get("attachmentId"):
                found.append((fn, p["body"]["attachmentId"]))
            _walk(p.get("parts"), found)

    def _download():
        msg = gmail.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()
        found: list[tuple[str, str]] = []
        _walk(msg.get("payload", {}).get("parts"), found)
        if not found:
            return None
        _, attach_id = found[0]
        attach = gmail.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=attach_id,
        ).execute()
        return base64.urlsafe_b64decode(attach["data"]).decode("utf-8-sig")

    return await asyncio.to_thread(_download)


async def _mark_as_read(gmail, msg_id: str):
    """Remove UNREAD label from an email."""
    import asyncio

    def _mark():
        gmail.users().messages().modify(
            userId="me", id=msg_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    await asyncio.to_thread(_mark)


def _classify_subject(subject: str) -> str:
    """Map subject pattern to import type."""
    lower = subject.lower()
    # Attendance check first — "Daily Attendance 9/9/2026 11:13:51 AM"
    # subject would otherwise fall through to "unknown."
    if "attendance" in lower or "absence" in lower:
        return "attendance"
    if "student" in lower:
        return "students"
    elif "admin" in lower:
        return "admins"
    elif "teacher" in lower:
        return "teachers"
    elif "enrollment" in lower:
        return "enrollments"
    elif "section" in lower:
        return "sections"
    return "unknown"


async def _process_csv(db, csv_text: str, import_type: str, subject: str) -> tuple[dict | None, list | None, dict | None, list | None, list | None]:
    """
    Process a Clever CSV and feed into the roster pipeline.

    Returns (teachers_dict, enrollments_list, section_meta, withdrawn_students, roster_changes)
    — collected data for cross-referencing and provisioning after all CSVs are processed.
    """
    from app.modules.roster import repository as repo
    from app.modules.roster.service import run_import
    from app.modules.roster.clever_service import (
        parse_students_csv, parse_enrollments_csv, parse_staff_csv,
        detect_csv_type, diff_roster, check_email_compliance,
        build_expected_email, expected_grad_year, _school_code,
    )

    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []

    if not headers:
        logger.warning(f"Empty CSV for subject '{subject}'")
        return None, None, None, None, None

    # Attendance path — MetaSolutions "Daily Attendance" report. Detected
    # by import_type (from filename "Absence List by Date Range.csv" or
    # subject "Daily Attendance ..."). Upserts into student_absences via
    # its own service module; doesn't participate in the Clever
    # returns-tuple downstream, so bail early with empty results.
    header_set = {h.strip() for h in headers}
    is_absence_shape = (
        "StudentNumber2" in header_set and "CalendarDate2" in header_set
        and "AbsenceType2" in header_set
    )
    if import_type == "attendance" or is_absence_shape:
        # Attendance ingest stripped in lite — the absence_service module
        # is gone. Just acknowledge + drop the CSV.
        logger.info(f"Absence CSV '{subject}' skipped — attendance module not in lite")
        return None, None, None, None, None

    # Use clever_service's type detection for more accurate classification
    detected_type = detect_csv_type(headers)

    if detected_type == "students" or import_type == "students":
        students = parse_students_csv(csv_text)
        if not students:
            logger.warning(f"No student records in CSV for '{subject}'")
            return None, None, None, None, None

        # Create import record
        imp = await repo.create_import(
            db, source="clever", started_by="system", filename=f"clever_{subject}",
        )

        # Fetch Google accounts once for status check during import
        from app.modules.settings.repository import get_setting_value as _gsv
        from app.modules.roster.clever_service import build_expected_email, expected_grad_year
        import re as _re
        student_domain = await _gsv(db, "google", "student_domain") or ""
        # {email: is_suspended} — importing suspended accounts too so the
        # roster page can distinguish "has a suspended account, needs
        # reactivating" from "no account at all, needs provisioning".
        # Pre-fix (isSuspended=false query) mis-tagged ~67 EPE students
        # as `google_status=missing` when their suspended accounts
        # already existed.
        google_status_by_email: dict[str, bool] = {}
        google_ou_by_email: dict[str, str] = {}
        if student_domain:
            try:
                from app.integrations.google.adapter import GoogleWorkspaceAdapter
                _google = GoogleWorkspaceAdapter(db)
                _accounts = await _google.get_active_accounts(
                    domain=student_domain, include_suspended=True,
                )
                google_status_by_email = {
                    a["email"].lower(): bool(a.get("suspended", False))
                    for a in _accounts
                }
                google_ou_by_email = {
                    a["email"].lower(): (a.get("org_unit_path") or "")
                    for a in _accounts
                }
                _active = sum(1 for s in google_status_by_email.values() if not s)
                _susp = sum(1 for s in google_status_by_email.values() if s)
                logger.info(f"Loaded {len(google_status_by_email)} Google accounts "
                            f"for status check ({_active} active, {_susp} suspended)")
            except Exception as e:
                logger.warning(f"Could not load Google accounts for import status check: {e}")

        # Load local email overrides — for students whose Clever email is
        # wrong (typo/legacy alias) and SIS can't or won't correct it. The
        # override takes precedence over Student_email so downstream
        # compliance checks + google_status match against the CORRECT
        # address. See a177 migration + roster_email_overrides table.
        overrides = {}
        try:
            _ov_rows = (await db.execute(sa_text(
                "SELECT sis_id, override_email FROM roster_email_overrides"
            ))).mappings().all()
            overrides = {r["sis_id"]: (r["override_email"] or "").strip().lower() for r in _ov_rows}
            if overrides:
                logger.info(f"Loaded {len(overrides)} roster_email_overrides")
        except Exception as e:
            # Table missing (pre-migration) or transient DB glitch — proceed
            # without overrides rather than blocking the whole import.
            logger.warning(f"Could not load roster_email_overrides: {e}")

        # Map Clever columns to our schema
        data = []
        for row in students:
            # Build address from parts
            street = row.get("Student_street", "").strip()
            city = row.get("Student_city", "").strip()
            state = row.get("Student_state", "").strip()
            zipcode = row.get("Student_zip", "").strip()
            address = ", ".join(filter(None, [street, city, f"{state} {zipcode}".strip()])) if street else ""

            # Primary contact — old vendor exposed Contact_name / Contact_phone
            # as named columns. MetaSolutions instead emits a single named
            # column "Contact_relations" followed by unnamed positional
            # columns for the first contact: [relation, name, phone,
            # phone_type, email, …]. DictReader stashes the excess values
            # under the `None` key. Fall through to whichever format is
            # actually present.
            contact_name = (row.get("Contact_name") or "").strip()
            contact_phone = (row.get("Contact_phone") or "").strip()
            if not contact_name or not contact_phone:
                extras = row.get(None) or []
                if not contact_name and len(extras) > 1:
                    contact_name = (extras[1] or "").strip()
                if not contact_phone and len(extras) > 2:
                    contact_phone = (extras[2] or "").strip()

            sis_id = row.get("Student_id", "").strip()
            email = row.get("Student_email", "").strip().lower()
            # Local override wins if configured — even when SIS provides
            # a value. Prevents daily re-tagging when SIS holds a typo.
            override_email = overrides.get(sis_id)
            if override_email:
                email = override_email
            first = row.get("First_name", "").strip()
            last = row.get("Last_name", "").strip()
            grade = row.get("Grade", "").strip()
            # Normalize zero-padded grades from MetaSolutions ("05" → "5").
            # Preserves 10/11/12/non-numeric codes untouched. Without
            # this, expected_grad_year() would compute 2005 for a 5th
            # grader and mass-tag legit emails as year_mismatch.
            if len(grade) == 2 and grade[0] == "0" and grade[1:].isdigit():
                grade = grade[1:]

            # Determine google_status and compliance
            google_status = ""
            email_compliant = None
            issue_tags = []

            if email and student_domain:
                if not email.endswith(f"@{student_domain}"):
                    google_status = "non_district"
                    email_compliant = False
                    issue_tags.append("wrong_domain")
                elif email in google_status_by_email:
                    google_status = "suspended" if google_status_by_email[email] else "active"
                    if google_status == "suspended":
                        issue_tags.append("suspended")
                    # Delegate to the canonical checker — tolerates ±2yr
                    # drift (retained students), IDM disambiguators
                    # (smithj262@), and hyphen normalization. Strict
                    # `==` here previously flooded ~1,800 legit emails
                    # with a bogus wrong_format tag every import.
                    from app.modules.roster.clever_service import check_email_compliance
                    compliance = check_email_compliance(
                        {
                            "Student_email": email,
                            "Last_name": last,
                            "First_name": first,
                            "Grade": grade,
                        },
                        ignored_emails=set(),
                        domain=student_domain,
                    )
                    if compliance.get("compliant"):
                        email_compliant = True
                    elif compliance.get("compliant") is False:
                        email_compliant = False
                        reason = compliance.get("reason") or "wrong_format"
                        tag = {
                            "year_mismatch":     "year_mismatch",
                            "name_mismatch":     "name_mismatch",
                            "format_mismatch":   "wrong_format",
                            "insufficient_data": "wrong_format",
                            "wrong_domain":      "wrong_domain",
                        }.get(reason, "wrong_format")
                        issue_tags.append(tag)
                else:
                    google_status = "missing"
                    issue_tags.append("no_google")

            data.append({
                "sis_id": row.get("Student_id", ""),
                "first_name": first,
                "last_name": last,
                "middle_name": row.get("Middle_name", ""),
                "email": email,
                "school": _school_code(row),
                "grade": grade,
                "dob": row.get("DOB") or row.get("Dob") or "",
                "status": "active",
                "parent_guardian": contact_name,
                "phone": contact_phone,
                "address": address,
                "google_status": google_status,
                "google_ou": google_ou_by_email.get(email) if email else None,
                "email_compliant": email_compliant,
                "issue_tags": ",".join(issue_tags) if issue_tags else "",
                # SWIS PBIS Person Import fields (a184 migration).
                # Stored verbatim from MetaSolutions — mappers live in
                # exports/swis_students.py, not here.
                "gender":          (row.get("Gender") or "").strip(),
                "race":            (row.get("Race") or "").strip(),
                "hispanic_latino": (row.get("Hispanic_Latino") or "").strip(),
                "ell_status":      (row.get("Ell_status") or "").strip(),
                "iep_status":      (row.get("Iep_status") or "").strip(),
            })
        # ── Snapshot before upsert for diff ──────────────────────────
        # Capture current DB state for schools in this import
        import json as _json
        from sqlalchemy import select as _select
        from app.modules.roster.models import RosterSnapshot

        import_schools = set()
        for row in data:
            s = row.get("school", "")
            if s:
                import_schools.add(s)

        old_snapshot = []
        if import_schools:
            old_q = await db.execute(
                _select(RosterSnapshot).where(
                    RosterSnapshot.school.in_(import_schools),
                    RosterSnapshot.status == "active",
                )
            )
            for s in old_q.scalars().all():
                old_snapshot.append({
                    "sis_id": s.sis_id,
                    "first_name": s.first_name,
                    "last_name": s.last_name,
                    "school": s.school,
                    "grade": s.grade or "",
                    "email": s.email or "",
                })

        import_result = await run_import(db, imp.id, data)
        # `withdrawn` is deferred to a district-wide pass in
        # poll_clever_imports; per-CSV withdrawal detection was the root
        # cause of the 2026-08-27 partial-CSV mass-deprovision incident.
        # `sis_ids_seen` is what we carry up so the caller can union
        # across every Students CSV in this run.
        sis_ids_seen = import_result.get("sis_ids_seen", set())

        # ── Diff: compare old snapshot vs new import data ──────────────
        from app.modules.settings.repository import get_setting_value
        school_code_map_raw = await get_setting_value(db, "roster", "school_code_map")
        school_code_map = None
        if school_code_map_raw:
            try:
                school_code_map = _json.loads(school_code_map_raw)
            except Exception:
                pass

        # Build new_students list from import data (same format as old_snapshot)
        new_snapshot = [
            {
                "sis_id": d["sis_id"],
                "first_name": d["first_name"],
                "last_name": d["last_name"],
                "school": d["school"],
                "grade": d.get("grade", ""),
                "email": d.get("email", ""),
            }
            for d in data if d.get("sis_id")
        ]

        roster_changes = diff_roster(old_snapshot, new_snapshot, school_code_map)

        # Write diff changes as RosterChange records (skip "removed" — handled by mark_students_inactive)
        for change in roster_changes:
            if change["change_type"] == "removed":
                continue  # Already logged by mark_students_inactive
            await repo.create_roster_change(
                db,
                import_id=imp.id,
                student_id=change["student_id"],
                change_type=change["change_type"],
                student_name=change["student_name"],
                school_code=change["school_code"],
                details=change["details"],
            )

        logger.info(
            f"Roster diff: {len([c for c in roster_changes if c['change_type'] == 'added'])} added, "
            f"{len([c for c in roster_changes if c['change_type'] == 'removed'])} removed, "
            f"{len([c for c in roster_changes if c['change_type'] == 'transferred'])} transferred, "
            f"{len([c for c in roster_changes if c['change_type'] == 'grade_change'])} grade, "
            f"{len([c for c in roster_changes if c['change_type'] == 'name_change'])} name"
        )

        ignored_emails = await repo.get_active_ignored_emails(db)
        domain = await get_setting_value(db, "roster", "student_email_domain") or await get_setting_value(db, "google", "student_domain") or ""

        # Email compliance checks — store violations as RosterChange records
        for student in students:
            compliance = check_email_compliance(student, ignored_emails, domain=domain)
            if compliance["compliant"] is False:
                await repo.create_roster_change(
                    db,
                    import_id=imp.id,
                    student_id=student.get("Student_id", ""),
                    change_type="email_noncompliant",
                    student_name=f"{student.get('First_name', '')} {student.get('Last_name', '')}".strip(),
                    school_code=_school_code(student, school_code_map),
                    details=_json.dumps({
                        "expected": compliance["expected"],
                        "actual": compliance["actual"],
                        "reason": compliance["reason"],
                    }),
                )

            # Missing email check
            actual_email = student.get("Student_email", "").strip()
            if not actual_email:
                sid = student.get("Student_id", "")
                first = student.get("First_name", "")
                last = student.get("Last_name", "")
                grade = student.get("Grade", "")
                grad_year = expected_grad_year(grade)
                suggested = build_expected_email(last, first, grad_year, domain=domain) if grad_year else None
                await repo.create_roster_change(
                    db,
                    import_id=imp.id,
                    student_id=sid,
                    change_type="missing_email",
                    student_name=f"{first} {last}".strip(),
                    school_code=_school_code(student, school_code_map),
                    details=_json.dumps({"suggested_email": suggested}),
                )

        await db.flush()
        logger.info(f"Processed {detected_type}/{import_type} from '{subject}'")
        # Fourth slot used to be per-CSV withdrawn_students. Now it's
        # per-CSV sis_ids_seen — the caller unions these across all
        # Students CSVs and does the withdrawal diff district-wide.
        return None, None, None, sis_ids_seen, roster_changes

    elif detected_type == "enrollments" or import_type == "enrollments":
        enrollments = parse_enrollments_csv(csv_text)
        logger.info(f"Collected {len(enrollments)} enrollment records from Clever")
        await db.flush()
        logger.info(f"Processed {detected_type}/{import_type} from '{subject}'")
        return None, enrollments, None, None, None

    elif detected_type == "staff" or import_type == "teachers" or import_type == "admins":
        staff = parse_staff_csv(csv_text)
        source = "admins" if import_type == "admins" else "teachers"
        logger.info(f"Collected {len(staff)} {source} records from Clever")

        # Store in clever_staff table for SIS status checks
        await _upsert_clever_staff(db, staff, source)

        # Index by Teacher_id for cross-referencing (teachers only)
        teachers = {}
        if source == "teachers":
            for t in staff:
                tid = t.get("Teacher_id", "").strip()
                if tid:
                    teachers[tid] = t

        await db.flush()
        logger.info(f"Processed {detected_type}/{import_type} from '{subject}'")
        return teachers if teachers else None, None, None, None, None

    elif detected_type == "sections" or import_type == "sections":
        # Sections CSV has period + course + term data AND the
        # authoritative teacher-id list for the section. The new
        # MetaSolutions format uses opaque section IDs like
        # ``SIS_B-302A-10-2026-SEM1`` — the old vendor's convention of
        # encoding teacher_id as the section_id suffix is gone. So we
        # capture Teacher_id / Teacher_2_id .. Teacher_10_id here and
        # let _build_student_teacher_map join enrollments to teachers
        # via section_id → teacher_ids instead of suffix-parsing.
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        section_meta = {}
        for row in rows:
            sec_id = row.get("Section_id", "").strip()
            if not sec_id:
                continue
            tids: list[str] = []
            for col in ("Teacher_id", "Teacher_2_id", "Teacher_3_id",
                        "Teacher_4_id", "Teacher_5_id", "Teacher_6_id",
                        "Teacher_7_id", "Teacher_8_id", "Teacher_9_id",
                        "Teacher_10_id"):
                tid = (row.get(col) or "").strip()
                if tid and tid not in tids:
                    tids.append(tid)
            section_meta[sec_id] = {
                "period": row.get("Period", "").strip(),
                "course_name": row.get("Course_name", "").strip(),
                "subject": row.get("Subject", "").strip(),
                "term_name": row.get("Term_name", "").strip(),
                "teacher_ids": tids,
            }
        logger.info(f"Collected {len(section_meta)} section records from Clever (period/course enrichment deferred)")
        await db.flush()
        logger.info(f"Processed {detected_type}/{import_type} from '{subject}'")
        return None, None, section_meta if section_meta else None, None, None

    await db.flush()
    logger.info(f"Processed {detected_type}/{import_type} from '{subject}'")
    return None, None, None, None, None


async def _enrich_sections(db, section_meta: dict):
    """Apply period + course_name + term_name from sections CSV to
    student_teachers table."""
    from sqlalchemy import text
    updated = 0
    for sec_id, meta in section_meta.items():
        if meta["period"] or meta["course_name"] or meta.get("term_name"):
            await db.execute(text(
                "UPDATE student_teachers SET "
                " period = COALESCE(:period, period), "
                " course_name = COALESCE(:course, course_name), "
                " term_name = COALESCE(:term, term_name) "
                "WHERE section_name = :sec"
            ).bindparams(
                period=meta["period"] or None,
                course=meta["course_name"] or None,
                term=meta.get("term_name") or None,
                sec=sec_id,
            ))
            updated += 1
    await db.flush()
    logger.info(
        f"Enriched {updated} sections with period/course/term data "
        f"from {len(section_meta)} section records"
    )


async def _upsert_clever_staff(db, staff_rows: list[dict], source: str):
    """
    Upsert Clever teacher/admin records into clever_staff table.
    Used for SIS status checks in the staff directory.
    """
    from sqlalchemy import text
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    count = 0
    for row in staff_rows:
        email = (row.get("Teacher_email") or row.get("Admin_email") or row.get("Staff_email") or "").strip().lower()
        if not email:
            continue
        clever_id = row.get("Teacher_id") or row.get("Admin_id") or row.get("Staff_id") or ""
        first = row.get("First_name", "").strip()
        last = row.get("Last_name", "").strip()
        title = row.get("Title", "").strip()
        school = row.get("School_id", "").strip()

        await db.execute(text("""
            INSERT INTO clever_staff (clever_id, email, first_name, last_name, title, school_id, source, synced_at)
            VALUES (:clever_id, :email, :first, :last, :title, :school, :source, :now)
            ON CONFLICT (email) DO UPDATE SET
                clever_id = EXCLUDED.clever_id,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                title = EXCLUDED.title,
                school_id = EXCLUDED.school_id,
                source = EXCLUDED.source,
                synced_at = EXCLUDED.synced_at
        """), {
            "clever_id": clever_id, "email": email, "first": first,
            "last": last, "title": title, "school": school,
            "source": source, "now": now,
        })
        count += 1

    logger.info(f"Upserted {count} {source} records into clever_staff")


async def _build_student_teacher_map(
    db, enrollments: list[dict], teachers: dict,
    section_meta: dict | None = None,
):
    """
    Cross-reference enrollment and teacher data to build student→teacher mapping.

    Preferred (MetaSolutions/new): join via ``section_meta[section_id].teacher_ids``
    which is populated from the Sections CSV's Teacher_id / Teacher_2_id …
    columns.

    Fallback (legacy): the old vendor encoded teacher_id as the suffix of
    section_id (e.g. ``0101MALO`` → ``MALO``). If section_meta doesn't
    have a matching entry we drop back to that heuristic so a mid-
    migration state (one CSV old-format, another new-format) still
    partially resolves.

    Replaces the entire student_teachers table with fresh data each import.
    Handles multi-teacher sections (up to 10 co-teachers per row) so
    students get every teacher listed, not just the primary.
    """
    from app.modules.roster.clever_service import extract_teacher_id_from_section
    from app.modules.roster.models import StudentTeacher
    from app.modules.roster import repository as repo
    from sqlalchemy import delete

    teacher_ids = set(teachers.keys())
    section_meta = section_meta or {}
    records = []
    seen: set[tuple[str, str, str]] = set()   # (student_sid, section_id, teacher_id)

    # Cache SIS→internal id lookups to avoid re-querying the same student
    # across each of their section enrollments (a HS senior can have 8+
    # rows in enrollments.csv, one per period).
    student_cache: dict[str, object] = {}

    for row in enrollments:
        sid = row.get("student_id", "")
        sec = row.get("section_id", "")
        if not sid or not sec:
            continue

        # New path — section→teacher list from Sections CSV
        tids: list[str] = []
        meta = section_meta.get(sec)
        if meta and meta.get("teacher_ids"):
            tids = [t for t in meta["teacher_ids"] if t in teacher_ids]

        # Legacy fallback — extract teacher_id from the section_id suffix
        if not tids:
            legacy = extract_teacher_id_from_section(sec, teacher_ids)
            if legacy:
                tids = [legacy]

        if not tids:
            continue

        if sid not in student_cache:
            student_cache[sid] = await repo.get_student_by_sis_id(db, sid)
        student = student_cache[sid]
        if not student:
            continue

        for tid in tids:
            key = (sid, sec, tid)
            if key in seen:
                continue
            seen.add(key)
            t = teachers[tid]
            teacher_name = f"{t.get('First_name', '')} {t.get('Last_name', '')}".strip()
            teacher_email = t.get("Teacher_email", "")
            records.append(StudentTeacher(
                student_id=student.id,
                teacher_name=teacher_name,
                teacher_email=teacher_email,
                section_name=sec,
                school=student.school,
            ))

    if records:
        # Clear old mapping and insert fresh
        await db.execute(delete(StudentTeacher))
        db.add_all(records)
        await db.flush()
        logger.info(
            f"Updated student-teacher mapping: {len(records)} pairs from "
            f"{len(teachers)} teachers, {len(section_meta)} sections"
        )
