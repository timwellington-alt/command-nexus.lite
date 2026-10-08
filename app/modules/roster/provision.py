"""
Student roster provisioning/deprovisioning/transfer pipeline.

Handles Google account lifecycle triggered by Clever import diff:
- New students → create or reactivate Google accounts
- Withdrawn students → handled by existing deprovision pipeline in clever_import_job
- Transferred students → move to new school OU + guidance queue
- Name collisions → disambiguate or flag for admin review

ALL Google writes gated by roster.student_google_writes_enabled setting.
Every mutation audit-logged + api_log'd.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _normalize_name(s: str) -> str:
    """Fold a name to a canonical form for comparison.

    Two levels of collapse:
      1. Unicode NFD-normalize + drop combining marks — so `José` matches
         `Jose`, `Núñez` matches `Nunez`. Common in SIS ↔ Google pairs
         where Google's account-create pipeline stripped diacritics.
      2. Strip everything that isn't a-z0-9 — so `O'Brien` matches
         `OBrien`, `Anne-Marie` matches `Anne Marie` matches `AnneMarie`,
         `Smith Jr.` matches `Smith Jr` matches `SmithJr`. Google also
         strips apostrophes/hyphens when generating primary emails, so
         SIS records with punctuation frequently mismatch Google's
         stored name field even for the same person.
    Case handled implicitly by lowercasing before the strip.
    """
    import unicodedata as _ud
    import re as _re
    if not s:
        return ""
    folded = _ud.normalize("NFD", s.strip().lower())
    folded = "".join(ch for ch in folded if _ud.category(ch) != "Mn")
    return _re.sub(r"[^a-z0-9]", "", folded)


def _names_match_exactly(google_user: dict, first_name: str, last_name: str) -> bool:
    """Check if Google account name matches student name after normalization.

    Compares via ``_normalize_name`` — tolerates case, whitespace, hyphens,
    apostrophes, diacritics, nickname punctuation. Does NOT tolerate
    real spelling differences (Rachel vs Rachael) or nicknames (Kate vs
    Katherine) — those still fall through to the disambiguate branch.
    """
    g_first = _normalize_name(google_user.get("first_name") or "")
    g_last = _normalize_name(google_user.get("last_name") or "")
    s_first = _normalize_name(first_name)
    s_last = _normalize_name(last_name)
    # Empty on either side = not a real match. Prevents "" == "" from
    # linking a nameless Google account to a real SIS row.
    return bool(g_first) and bool(g_last) and g_first == s_first and g_last == s_last


def _names_close_match(google_user: dict, first_name: str, last_name: str) -> bool:
    """Check if names are close enough to flag for review (first 3 chars match)."""
    g_first = (google_user.get("first_name") or "").strip().lower()
    g_last = (google_user.get("last_name") or "").strip().lower()
    s_first = first_name.strip().lower()
    s_last = last_name.strip().lower()
    return (
        len(g_first) >= 3 and len(s_first) >= 3 and
        len(g_last) >= 3 and len(s_last) >= 3 and
        g_first[:3] == s_first[:3] and g_last[:3] == s_last[:3]
    )


async def _get_provisioning_settings(db: AsyncSession) -> dict | None:
    """
    Load all provisioning-related settings. Returns None if writes are disabled.
    """
    from app.modules.settings.repository import get_setting_value

    # Autopilot requires the single-mode setting = 'autopilot'. Legacy
    # fallback honors the old (provisioning_enabled, google_writes_enabled)
    # pair so a mid-upgrade deploy doesn't go silent.
    mode = (await get_setting_value(db, "roster", "student_provisioning_mode") or "").lower()
    if not mode:
        legacy_prov = (await get_setting_value(db, "roster", "student_provisioning_enabled") or "").lower()
        legacy_writes = (await get_setting_value(db, "roster", "student_google_writes_enabled") or "").lower()
        if legacy_prov == "true" and legacy_writes == "true":
            mode = "autopilot"
    if mode != "autopilot":
        return None

    student_domain = await get_setting_value(db, "google", "student_domain") or ""
    if not student_domain:
        logger.warning("Provisioning: google.student_domain not configured")
        return None

    pwd_template = await get_setting_value(db, "roster", "student_default_password") or "0000{SID_LAST4}"
    deprov_ou = await get_setting_value(db, "roster", "student_deprovision_ou") or ""

    # School → OU mapping
    ou_map_raw = await get_setting_value(db, "roster", "student_ou_map") or "{}"
    try:
        ou_map = json.loads(ou_map_raw)
    except Exception:
        ou_map = {}

    return {
        "student_domain": student_domain,
        "pwd_template": pwd_template,
        "deprov_ou": deprov_ou,
        "ou_map": ou_map,
    }


def _build_password(template: str, sis_id: str) -> str:
    """Build a student password from template and SIS ID."""
    sid_last4 = sis_id[-4:] if sis_id and len(sis_id) >= 4 else "0000"
    return template.replace("{SID_LAST4}", sid_last4).replace("{SID}", sis_id or "")


def _resolve_ou(school: str, ou_map: dict) -> str:
    """Resolve target OU for a school code."""
    return ou_map.get(school) or ou_map.get("*") or "/Users-Students"


async def provision_new_students(
    db: AsyncSession,
    changes: list[dict],
    import_id: int | None = None,
) -> dict:
    """
    Process 'added' changes — provision or reactivate Google accounts.

    Args:
        db: Database session
        changes: List of diff change dicts with change_type='added'
        import_id: Import record ID for change tracking

    Returns dict with counts: provisioned, reactivated, review_flagged, skipped, errors
    """
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.integrations.api_log import log_api_command
    from app.modules.roster.clever_service import build_expected_email, expected_grad_year
    from app.modules.roster import repository as repo
    from app.audit.service import log_action

    result = {"provisioned": 0, "reactivated": 0, "review_flagged": 0, "skipped": 0, "errors": 0, "details": []}

    settings = await _get_provisioning_settings(db)
    if not settings:
        result["skipped_reason"] = "Google writes disabled or not configured"
        return result

    gws = GoogleWorkspaceAdapter(db)
    domain = settings["student_domain"]
    ou_map = settings["ou_map"]
    pwd_template = settings["pwd_template"]

    added_changes = [c for c in changes if c.get("change_type") == "added"]
    if not added_changes:
        return result

    for change in added_changes:
        sid = change.get("student_id", "")
        details = json.loads(change.get("details", "{}"))
        student_data = details.get("student_data", details)
        first_name = student_data.get("first_name", "")
        last_name = student_data.get("last_name", "")
        grade = student_data.get("grade", "")
        school = student_data.get("school", "") or change.get("school_code", "")
        existing_email = student_data.get("email", "")

        if not first_name or not last_name:
            result["skipped"] += 1
            continue

        # Build expected email
        grad_year = expected_grad_year(grade) if grade else None
        if not grad_year:
            result["skipped"] += 1
            result["details"].append({"sis_id": sid, "action": "skipped", "reason": "no_grad_year"})
            continue

        email = existing_email if existing_email and existing_email.endswith(f"@{domain}") else None
        if not email:
            email = build_expected_email(last_name, first_name, grad_year, domain=domain)

        target_ou = _resolve_ou(school, ou_map)
        temp_password = _build_password(pwd_template, sid)

        try:
            # Check if email exists in Google
            existing = await gws.get_user(email)
            log_api_command(
                system="google", action="get_user", target=email,
                response_status="found" if existing else "not_found",
                caller="provision.provision_new_students",
            )

            if not existing:
                # Create new account
                create_result = await gws.create_account(
                    email=email, first_name=first_name, last_name=last_name,
                    org_unit=target_ou, temp_password=temp_password,
                )
                log_api_command(
                    system="google", action="create_account", target=email,
                    payload=json.dumps({"ou": target_ou, "sis_id": sid}),
                    response_status="ok" if create_result.success else "error",
                    response_summary=create_result.error or "success",
                    caller="provision.provision_new_students",
                )

                if create_result.success:
                    # Set student ID
                    sid_result = await gws.set_student_id(email, sid)
                    log_api_command(
                        system="google", action="set_student_id", target=email,
                        payload=json.dumps({"student_id": sid}),
                        response_status="ok" if sid_result.success else "error",
                        caller="provision.provision_new_students",
                    )

                    # Log to google_change_log
                    await repo.create_google_change(
                        db, actor="system", action="create_account",
                        target_email=email, student_id=sid,
                        after_state=json.dumps({"ou": target_ou, "email": email}),
                    )

                    # Mark the roster change as provisioned
                    if import_id:
                        await _mark_change_provisioned(db, import_id, sid)

                    result["provisioned"] += 1
                    result["details"].append({"sis_id": sid, "email": email, "action": "created"})

                    await log_action(
                        db, actor="system", action="roster.accounts.provision.auto",
                        module="roster", target=f"{first_name} {last_name} ({sid})",
                        details=json.dumps({"email": email, "ou": target_ou}),
                    )
                else:
                    result["errors"] += 1
                    result["details"].append({"sis_id": sid, "email": email, "action": "create_failed", "error": create_result.error})

            elif existing:
                existing_sid = existing.get("student_id") or ""
                g_suspended = existing.get("suspended", False)

                if existing_sid == sid:
                    # Same student returning — reactivate
                    actions = []
                    if g_suspended:
                        reactivate_result = await gws.reactivate_account(email)
                        log_api_command(
                            system="google", action="reactivate_account", target=email,
                            response_status="ok" if reactivate_result.success else "error",
                            caller="provision.provision_new_students",
                        )
                        if reactivate_result.success:
                            actions.append("reactivated")

                    # Move to correct OU
                    current_ou = existing.get("org_unit_path", "")
                    if current_ou != target_ou:
                        move_result = await gws.move_user_ou(email, target_ou)
                        log_api_command(
                            system="google", action="move_user_ou", target=email,
                            payload=json.dumps({"from": current_ou, "to": target_ou}),
                            response_status="ok" if move_result.success else "error",
                            caller="provision.provision_new_students",
                        )
                        if move_result.success:
                            actions.append(f"moved:{current_ou}->{target_ou}")

                    await repo.create_google_change(
                        db, actor="system", action="reactivate_account",
                        target_email=email, student_id=sid,
                        before_state=json.dumps({"suspended": g_suspended, "ou": current_ou}),
                        after_state=json.dumps({"suspended": False, "ou": target_ou}),
                    )

                    if import_id:
                        await _mark_change_provisioned(db, import_id, sid)

                    result["reactivated"] += 1
                    result["details"].append({"sis_id": sid, "email": email, "action": "reactivated", "actions": actions})

                    await log_action(
                        db, actor="system", action="roster.accounts.reactivate.auto",
                        module="roster", target=f"{first_name} {last_name} ({sid})",
                        details=json.dumps({"email": email, "actions": actions}),
                    )

                elif not existing_sid:
                    # No SID on existing account — check names.
                    #
                    # Both branches formerly parked the row in "account_review"
                    # limbo (Phoebe Artressia sat there for hours), but the
                    # import path is already gated by
                    # ``student_google_writes_enabled``: if that's on, the
                    # operator has already opted into automated writes. So:
                    #   - Exact name match  → auto-link (same actions as
                    #     the returning-student branch above: stamp SID,
                    #     unsuspend if needed, move OU if wrong).
                    #   - Any name mismatch → disambiguate. The old
                    #     "close match" flag trapped siblings (Malkom vs
                    #     Malaki Allen) far more often than it caught
                    #     Rachel/Rachael spelling variants, so the safer
                    #     move is a fresh disambiguated address.
                    if _names_match_exactly(existing, first_name, last_name):
                        actions = ["stamped SID"]
                        sid_result = await gws.set_student_id(email, sid)
                        log_api_command(
                            system="google", action="set_student_id", target=email,
                            payload=json.dumps({"student_id": sid, "auto_link": True}),
                            response_status="ok" if sid_result.success else "error",
                            caller="provision.provision_new_students.auto_link",
                        )
                        if g_suspended:
                            reactivate_result = await gws.reactivate_account(email)
                            log_api_command(
                                system="google", action="reactivate_account", target=email,
                                response_status="ok" if reactivate_result.success else "error",
                                caller="provision.provision_new_students.auto_link",
                            )
                            if reactivate_result.success:
                                actions.append("unsuspended")
                        current_ou = existing.get("org_unit_path", "")
                        if current_ou != target_ou:
                            move_result = await gws.move_user_ou(email, target_ou)
                            log_api_command(
                                system="google", action="move_user_ou", target=email,
                                payload=json.dumps({"from": current_ou, "to": target_ou}),
                                response_status="ok" if move_result.success else "error",
                                caller="provision.provision_new_students.auto_link",
                            )
                            if move_result.success:
                                actions.append(f"moved:{current_ou}->{target_ou}")

                        await repo.create_google_change(
                            db, actor="system", action="auto_link_account",
                            target_email=email, student_id=sid,
                            before_state=json.dumps({
                                "suspended": g_suspended, "ou": current_ou,
                                "employee_id": None,
                            }),
                            after_state=json.dumps({
                                "suspended": False, "ou": target_ou,
                                "employee_id": sid,
                            }),
                        )
                        if import_id:
                            await _mark_change_provisioned(db, import_id, sid)
                        result["reactivated"] += 1
                        result["details"].append({
                            "sis_id": sid, "email": email,
                            "action": "auto_linked", "actions": actions,
                            "trigger": "exact_name_no_sid",
                        })
                        await log_action(
                            db, actor="system",
                            action="roster.accounts.auto_linked.auto",
                            module="roster",
                            target=f"{first_name} {last_name} ({sid})",
                            details=json.dumps({
                                "email": email, "actions": actions,
                                "google_name": existing.get("full_name", ""),
                            }),
                        )

                    else:
                        # Name mismatch (whether close or wildly different) →
                        # disambiguate. Sibling collisions are far more common
                        # than spelling-variant-of-same-person.
                        new_email = build_expected_email(last_name, first_name, grad_year, domain=domain, disambiguate=True)
                        await _create_disambiguated(
                            db, gws, new_email, first_name, last_name, target_ou,
                            temp_password, sid, import_id, result,
                        )

                else:
                    # Different SID — name collision — disambiguate
                    new_email = build_expected_email(last_name, first_name, grad_year, domain=domain, disambiguate=True)
                    await _create_disambiguated(
                        db, gws, new_email, first_name, last_name, target_ou,
                        temp_password, sid, import_id, result,
                    )

        except Exception as e:
            logger.error(f"Provisioning failed for {sid}: {e}")
            result["errors"] += 1
            result["details"].append({"sis_id": sid, "action": "error", "error": str(e)[:200]})

    return result


async def _create_disambiguated(
    db, gws, email, first_name, last_name, target_ou,
    temp_password, sid, import_id, result,
):
    """Create account with disambiguated email. Updates result dict in place."""
    from app.integrations.api_log import log_api_command
    from app.modules.roster import repository as repo
    from app.audit.service import log_action

    # Check if disambiguated email also exists
    exists = await gws.check_email_exists(email)
    if exists:
        result["errors"] += 1
        result["details"].append({"sis_id": sid, "email": email, "action": "error", "error": "disambiguated_email_also_exists"})
        return

    create_result = await gws.create_account(
        email=email, first_name=first_name, last_name=last_name,
        org_unit=target_ou, temp_password=temp_password,
    )
    log_api_command(
        system="google", action="create_account", target=email,
        payload=json.dumps({"ou": target_ou, "sis_id": sid, "disambiguated": True}),
        response_status="ok" if create_result.success else "error",
        response_summary=create_result.error or "success",
        caller="provision._create_disambiguated",
    )

    if create_result.success:
        await gws.set_student_id(email, sid)
        log_api_command(
            system="google", action="set_student_id", target=email,
            payload=json.dumps({"student_id": sid}),
            response_status="ok", caller="provision._create_disambiguated",
        )

        await repo.create_google_change(
            db, actor="system", action="create_account",
            target_email=email, student_id=sid,
            after_state=json.dumps({"ou": target_ou, "email": email, "disambiguated": True}),
        )

        if import_id:
            await _mark_change_provisioned(db, import_id, sid)

        result["provisioned"] += 1
        result["details"].append({"sis_id": sid, "email": email, "action": "created_disambiguated"})

        await log_action(
            db, actor="system", action="roster.accounts.provision.auto",
            module="roster", target=f"{first_name} {last_name} ({sid})",
            details=json.dumps({"email": email, "ou": target_ou, "disambiguated": True}),
        )
    else:
        result["errors"] += 1
        result["details"].append({"sis_id": sid, "email": email, "action": "create_failed", "error": create_result.error})


async def handle_transfers(
    db: AsyncSession,
    changes: list[dict],
    import_id: int | None = None,
) -> dict:
    """
    Process 'transferred' changes — move Google accounts to new OU + queue guidance.

    Args:
        db: Database session
        changes: List of diff change dicts with change_type='transferred'
        import_id: Import record ID for change tracking

    Returns dict with counts: moved, guidance_queued, skipped, errors
    """
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.integrations.api_log import log_api_command
    from app.modules.roster import repository as repo
    from app.audit.service import log_action

    result = {"moved": 0, "guidance_queued": 0, "skipped": 0, "errors": 0}

    settings = await _get_provisioning_settings(db)

    transferred = [c for c in changes if c.get("change_type") == "transferred"]
    if not transferred:
        return result

    gws = GoogleWorkspaceAdapter(db) if settings else None

    for change in transferred:
        sid = change.get("student_id", "")
        details = json.loads(change.get("details", "{}"))
        school_info = details.get("school", {})
        from_school = school_info.get("from", "") if isinstance(school_info, dict) else ""
        to_school = school_info.get("to", "") if isinstance(school_info, dict) else ""
        email = details.get("email", "")

        # Move Google account to new OU if writes enabled
        if settings and gws and email and email.endswith(f"@{settings['student_domain']}"):
            target_ou = _resolve_ou(to_school, settings["ou_map"])
            try:
                move_result = await gws.move_user_ou(email, target_ou)
                log_api_command(
                    system="google", action="move_user_ou", target=email,
                    payload=json.dumps({"from_school": from_school, "to_school": to_school, "ou": target_ou}),
                    response_status="ok" if move_result.success else "error",
                    response_summary=move_result.error or "success",
                    caller="provision.handle_transfers",
                )

                if move_result.success:
                    await repo.create_google_change(
                        db, actor="system", action="move_ou",
                        target_email=email, student_id=sid,
                        before_state=json.dumps({"school": from_school}),
                        after_state=json.dumps({"school": to_school, "ou": target_ou}),
                    )
                    result["moved"] += 1

                    await log_action(
                        db, actor="system", action="roster.accounts.transfer.auto",
                        module="roster", target=f"{change.get('student_name', '')} ({sid})",
                        details=json.dumps({"email": email, "from": from_school, "to": to_school, "ou": target_ou}),
                    )
                else:
                    result["errors"] += 1
            except Exception as e:
                logger.error(f"Transfer OU move failed for {sid}: {e}")
                result["errors"] += 1
        elif not settings:
            result["skipped"] += 1

        # Queue guidance entry at receiving school
        try:
            student = await repo.get_student_by_sis_id(db, sid)
            if student:
                from sqlalchemy import text
                existing_queue = await db.execute(text(
                    "SELECT id FROM guidance_queue WHERE student_id = :sid AND status IN ('open', 'in_progress') AND category = 'transfer'"
                ).bindparams(sid=student.id))
                if not existing_queue.first():
                    await repo.create_queue_item(
                        db,
                        student_id=student.id,
                        school=to_school,
                        category="transfer",
                        priority="normal",
                        status="open",
                        created_by="system",
                        notes=f"Transferred from {from_school} to {to_school}",
                    )
                    result["guidance_queued"] += 1
        except Exception as e:
            logger.warning(f"Transfer guidance queue failed for {sid}: {e}")

    return result


async def handle_grade_changes(
    db: AsyncSession,
    changes: list[dict],
    import_id: int | None = None,
) -> dict:
    """Process ``change_type='grade_change'`` diffs by RENAMING the
    student's existing Google account when the grade change shifts
    their expected email suffix (i.e. a retention or advancement that
    changes graduation year).

    Policy (Tim, 2026-08-25): never mint a new account for the same
    student when their grad year moves. Rename in place so their Drive,
    Classroom membership, and calendars ride the same account. This
    prevents the ~114 duplicate accounts we found in the PHS/PES/EPE
    orphan audit — every one of them was a pre-Nexus manual create in
    response to a grade change instead of a rename.

    Per-diff logic:
      1. Compute expected email from NEW grade
      2. If expected email already exists in Google → nothing to do
      3. Else look up the student's current account via SID (externalIds).
         If found and its primaryEmail differs from the expected → rename.
      4. Fall through silently on any ambiguity so we don't blow up an
         import cycle.

    Returns counts. Skips silently if writes are disabled or the diff
    doesn't contain enough info.
    """
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.modules.roster.clever_service import build_expected_email, expected_grad_year
    from app.audit.service import log_action

    result = {"renamed": 0, "already_correct": 0, "no_existing_account": 0,
              "skipped": 0, "errors": 0}
    settings = await _get_provisioning_settings(db)
    if not settings:
        result["skipped_reason"] = "Google writes disabled or not configured"
        return result

    grade_changes = [c for c in changes if c.get("change_type") == "grade_change"]
    if not grade_changes:
        return result

    domain = settings["student_domain"]
    gws = GoogleWorkspaceAdapter(db)

    for change in grade_changes:
        sid = change.get("student_id", "")
        details = json.loads(change.get("details", "{}"))
        student_data = details.get("student_data", details)
        first_name = student_data.get("first_name") or ""
        last_name  = student_data.get("last_name")  or ""
        # Grade change payload shape: {"grade": {"from": "X", "to": "Y"}}
        new_grade = ""
        grade_field = details.get("grade")
        if isinstance(grade_field, dict):
            new_grade = str(grade_field.get("to") or "").strip()
        elif isinstance(grade_field, str):
            new_grade = grade_field.strip()
        if not (sid and first_name and last_name and new_grade):
            result["skipped"] += 1; continue

        grad_year = expected_grad_year(new_grade)
        if not grad_year:
            result["skipped"] += 1; continue

        expected_email = build_expected_email(last_name, first_name, grad_year, domain=domain)
        try:
            existing_at_new = await gws.get_user(expected_email)
        except Exception as e:
            logger.warning(f"grade_change get_user({expected_email!r}) failed: {e}")
            existing_at_new = None

        if existing_at_new:
            result["already_correct"] += 1
            continue

        # Find the student's current Google account via externalId=SID.
        try:
            hits = await gws.list_users(
                query=f"externalId={sid}", max_results=5,
            )
        except Exception as e:
            logger.warning(f"grade_change list_users(externalId={sid}) failed: {e}")
            hits = []
        # Filter to district-domain accounts we can actually rename
        district_users = [u for u in hits if (u.get("primaryEmail") or "").endswith(f"@{domain}")]
        if not district_users:
            result["no_existing_account"] += 1
            continue
        if len(district_users) > 1:
            logger.warning(
                f"grade_change: SID {sid} matched {len(district_users)} district_users "
                f"accounts — ambiguous, skipping rename. Accounts: "
                f"{[u.get('primaryEmail') for u in district_users]}"
            )
            result["skipped"] += 1
            continue

        current = district_users[0]
        old_email = current.get("primaryEmail") or ""
        if not old_email or old_email == expected_email:
            result["already_correct"] += 1
            continue

        # Rename the old account to the expected email — Google keeps
        # the old address as a nonEditableAlias for ~72h so no login
        # disruption during propagation.
        try:
            r = await gws.rename_account(old_email, expected_email)
            if not r.success:
                result["errors"] += 1
                logger.warning(f"grade_change rename {old_email}→{expected_email} failed: {r.error}")
                continue
            await log_action(
                db, actor="system",
                action="roster.accounts.rename.grade_change",
                module="roster",
                target=f"{first_name} {last_name} ({sid})",
                details=json.dumps({
                    "from": old_email, "to": expected_email,
                    "new_grade": new_grade, "grad_year": grad_year,
                    "reason": "grade change shifted expected email suffix — renamed in place instead of minting a new account",
                }),
            )
            result["renamed"] += 1
            logger.info(
                f"grade_change rename: {first_name} {last_name} ({sid}) "
                f"{old_email} → {expected_email}"
            )
        except Exception as e:
            result["errors"] += 1
            logger.warning(f"grade_change rename raised for {sid}: {e}")

    return result


async def resolve_account_review(
    db: AsyncSession,
    change_id: int,
    actor: str,
) -> dict:
    """
    IT confirms an account_review match is correct:
    - Reactivates the account if suspended
    - Writes the SID
    - Moves to correct OU
    - Marks the RosterChange as reviewed
    - Audit logged
    """
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.integrations.api_log import log_api_command
    from app.modules.roster import repository as repo
    from app.audit.service import log_action
    from sqlalchemy import select
    from app.modules.roster.models import RosterChange

    # Load the change record
    change_result = await db.execute(
        select(RosterChange).where(RosterChange.id == change_id)
    )
    change = change_result.scalar_one_or_none()
    if not change:
        return {"status": "error", "detail": "Change record not found"}

    if change.change_type != "account_review":
        return {"status": "error", "detail": f"Change type is '{change.change_type}', not 'account_review'"}

    if change.reviewed:
        return {"status": "error", "detail": "Already reviewed"}

    details = json.loads(change.details) if change.details else {}
    email = details.get("email", "")
    sid = change.student_id

    if not email or not sid:
        return {"status": "error", "detail": "Missing email or student ID in change record"}

    settings = await _get_provisioning_settings(db)
    if not settings:
        return {"status": "error", "detail": "Google writes disabled"}

    gws = GoogleWorkspaceAdapter(db)
    actions = []

    try:
        # Reactivate if suspended
        if details.get("google_suspended"):
            reactivate_result = await gws.reactivate_account(email)
            log_api_command(
                system="google", action="reactivate_account", target=email,
                response_status="ok" if reactivate_result.success else "error",
                caller=f"provision.resolve_account_review:{actor}",
            )
            if reactivate_result.success:
                actions.append("reactivated")

        # Set student ID
        sid_result = await gws.set_student_id(email, sid)
        log_api_command(
            system="google", action="set_student_id", target=email,
            payload=json.dumps({"student_id": sid}),
            response_status="ok" if sid_result.success else "error",
            caller=f"provision.resolve_account_review:{actor}",
        )
        if sid_result.success:
            actions.append("sid_set")

        # Move to correct OU
        school = change.school_code or ""
        if school:
            target_ou = _resolve_ou(school, settings["ou_map"])
            current_ou = details.get("google_ou", "")
            if current_ou != target_ou:
                move_result = await gws.move_user_ou(email, target_ou)
                log_api_command(
                    system="google", action="move_user_ou", target=email,
                    payload=json.dumps({"from": current_ou, "to": target_ou}),
                    response_status="ok" if move_result.success else "error",
                    caller=f"provision.resolve_account_review:{actor}",
                )
                if move_result.success:
                    actions.append(f"moved:{current_ou}->{target_ou}")

        # Google change log
        await repo.create_google_change(
            db, actor=actor, action="resolve_account_review",
            target_email=email, student_id=sid,
            before_state=json.dumps(details),
            after_state=json.dumps({"actions": actions}),
        )

        # Mark reviewed
        change.reviewed = True
        change.google_provisioned = True

        await log_action(
            db, actor=actor, action="roster.accounts.review_resolved",
            module="roster", target=f"{change.student_name} ({sid})",
            details=json.dumps({"email": email, "actions": actions, "change_id": change_id}),
        )

        return {"status": "ok", "email": email, "actions": actions}

    except Exception as e:
        logger.error(f"Account review resolution failed for change {change_id}: {e}")
        return {"status": "error", "detail": str(e)[:200]}


async def _mark_change_provisioned(db: AsyncSession, import_id: int, student_id: str):
    """Mark an 'added' RosterChange as Google-provisioned."""
    from sqlalchemy import select, and_
    from app.modules.roster.models import RosterChange

    result = await db.execute(
        select(RosterChange).where(and_(
            RosterChange.import_id == import_id,
            RosterChange.student_id == student_id,
            RosterChange.change_type == "added",
        ))
    )
    change = result.scalar_one_or_none()
    if change:
        change.google_provisioned = True
