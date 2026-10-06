"""Nightly student roster ↔ Google Workspace reconciliation.

The existing pipeline (clever_import_job) is *diff-based*: it only touches
Google when a kid transitions in an SIS import ("added" or "withdrawn since
yesterday"). That leaves two silent-drift failure modes uncovered:

  Direction A — active-in-SIS but archived/wrong-OU on the Google side.
    Google itself (via built-in inactivity rules) or admin-console cleanup
    can move never-logged-in student accounts to /Archived Accounts. The
    diff pipeline never revisits them, so the drift accumulates. Today's
    manual sweep caught 14 such kids and moved 1 (Zylan Wood, EPE→PHS).

  Direction B — active-in-Google but not in the current SIS roster.
    Withdrawn/graduated kids whose withdrawal day the pipeline missed
    (Nexus not running, malformed CSV, feed gap) sit active in Google
    forever, consuming Ed+ seats. Today's manual sweep caught 297 such
    accounts (107 graduated + 171 dup orphans + 124 alumni + 2 confirmed
    not-in-SIS).

This job runs both sweeps nightly, after the SIS import completes.

Safety:
  * DRY-RUN by default. `roster.reconcile_dry_run` must be flipped to
    'false' before any Google writes happen. Even then, `roster.
    reconcile_enabled` gates the whole job.
  * Directory sanity gate — abort if roster_snapshots active count drops
    below `roster.reconcile_min_roster_active` (default 1500). Prevents
    a mid-import empty-CSV window from misfiring. See
    [[feedback_dir_diff_sanity_gate]].
  * Hard cap — `roster.reconcile_max_per_run` (default 50) bounds
    Direction B archives per single run. Overflow gets queued rather
    than applied.
  * Lastlogin floor — `roster.reconcile_lastlogin_min_days` (default
    180) — never archive a student whose Google account has logged in
    within N days, even if SIS forgot about them.
  * Every candidate is persisted to `student_reconcile_candidates`
    regardless of mode. Every apply is audit-logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

ARCHIVE_OU_DEFAULT = "/Archived Accounts/Students"


async def reconcile_student_google_state(ctx: dict) -> dict:
    """Nightly student ↔ Google reconciliation. Idempotent."""
    result: dict = {
        "job": "reconcile_student_google_state",
        "mode": None,
        "aborted": None,
        "dir_a_scanned": 0, "dir_a_candidates": 0, "dir_a_applied": 0,
        "dir_b_scanned": 0, "dir_b_candidates": 0, "dir_b_applied": 0,
        "errors": [],
    }

    from app.db.engine import AsyncSessionLocal
    from app.modules.settings.repository import get_setting_value as _gsv

    # ── 1. Load config + gate ─────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        enabled = (await _gsv(db, "roster", "reconcile_enabled") or "false").lower()
        if enabled != "true":
            result["aborted"] = "reconcile_enabled=false (feature off)"
            return result

        # Schedule window gate — scheduled runs only fire during a
        # configured weekday + hour, so a full reconcile can't hit the
        # network in the middle of class. Manual invocations (job_id
        # not starting "scheduled:") bypass this so an operator can
        # still fire it on demand from the settings page.
        #
        # Settings (both optional; defaults to "Fri 18:00" — Friday
        # evening after school when a bad mass-action is easiest to
        # notice + reverse before Monday):
        #   roster.reconcile_weekday — 0=Mon … 6=Sun (default 4=Fri)
        #   roster.reconcile_hour    — 0-23 local, default 18
        # Cadence at the scheduler layer is now hourly, so the gate
        # only allows one match per week.
        is_scheduled = ctx.get("job_id", "").startswith("scheduled:")
        if is_scheduled:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo
            tz_name = await _gsv(db, "branding", "timezone") or "America/New_York"
            try:
                target_wd = int(await _gsv(db, "roster", "reconcile_weekday") or "4")
            except ValueError:
                target_wd = 4
            try:
                target_hr = int(await _gsv(db, "roster", "reconcile_hour") or "18")
            except ValueError:
                target_hr = 18
            now_local = _dt.now(ZoneInfo(tz_name))
            if now_local.weekday() != target_wd or now_local.hour != target_hr:
                result["aborted"] = (
                    f"not in scheduled window (want weekday={target_wd}, "
                    f"hour={target_hr}; got weekday={now_local.weekday()}, "
                    f"hour={now_local.hour} {tz_name})"
                )
                return result

        dry_run = (await _gsv(db, "roster", "reconcile_dry_run") or "true").lower() != "false"
        result["mode"] = "dry_run" if dry_run else "live"

        stale_days = int(await _gsv(db, "roster", "reconcile_stale_days") or "30")
        lastlogin_min_days = int(
            await _gsv(db, "roster", "reconcile_lastlogin_min_days") or "180"
        )
        max_per_run = int(await _gsv(db, "roster", "reconcile_max_per_run") or "50")
        min_roster_active = int(
            await _gsv(db, "roster", "reconcile_min_roster_active") or "1500"
        )
        deprov_ou = (await _gsv(db, "roster", "student_deprovision_ou")
                     or ARCHIVE_OU_DEFAULT)
        try:
            ou_map = json.loads(await _gsv(db, "roster", "student_ou_map") or "{}")
        except Exception:
            ou_map = {}
        if not ou_map:
            result["aborted"] = "student_ou_map not configured"
            return result

        # District domain allowlist. Direction A records a candidate rather
        # than errors when the SIS-supplied email is outside these domains
        # (Tim's rule: don't skip — the kid is district-attached in the SIS
        # so the wrong email likely just needs updating; surface for review).
        # Falls back to the student_email_domain if the new key isn't set.
        district_domains_raw = (
            await _gsv(db, "roster", "reconcile_district_email_domains")
            or await _gsv(db, "roster", "student_email_domain")
            or ""
        )
        district_domains = {
            d.strip().lower().lstrip("@")
            for d in district_domains_raw.split(",") if d.strip()
        }

        # Direction B — explicit ignore list of shared / non-student
        # accounts that live in student OUs (peslibrary@, testphs@,
        # etc.). These should never be classed as archive candidates.
        # Also a name-shape regex — student emails always end in a
        # 2- or 3-digit grad-year suffix; anything without digits is a
        # shared account and gets a distinct decision (never archived).
        import re as _re
        ignore_raw = await _gsv(db, "roster", "reconcile_ignore_emails") or ""
        ignore_emails = {
            e.strip().lower() for e in ignore_raw.split(",") if e.strip()
        }
        student_local_re = _re.compile(r"[a-z]\d{2,3}$")  # ends w/ letter+2-3 digits

        # Directory sanity gate — see [[feedback_dir_diff_sanity_gate]].
        roster_active_count = (await db.execute(text(
            "SELECT count(*) FROM roster_snapshots WHERE status='active'"
        ))).scalar_one()
        if roster_active_count < min_roster_active:
            result["aborted"] = (
                f"roster active count {roster_active_count} below floor "
                f"{min_roster_active} (mid-import window?)"
            )
            # Persist an aborted run row so it shows up in the audit view.
            await _record_run_row(db, mode=result["mode"],
                                  aborted_reason=result["aborted"],
                                  roster_active_count=roster_active_count,
                                  counts=result)
            await db.commit()
            return result

    # ── 2. Open a run row we'll update throughout ──────────────────────
    async with AsyncSessionLocal() as db:
        run_id = await _record_run_row(
            db, mode=result["mode"], aborted_reason=None,
            roster_active_count=roster_active_count, counts=result,
        )
        await db.commit()
    result["run_id"] = run_id

    # ── 3. Preload state ──────────────────────────────────────────────
    # Roster: build lookup keyed by lowercased email AND by SIS id.
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT sis_id, first_name, last_name, email, school, grade
            FROM roster_snapshots
            WHERE status='active' AND email IS NOT NULL AND email <> ''
        """))).mappings().all()
    roster_by_email: dict[str, dict] = {}
    roster_sis_ids: set[str] = set()
    for r in rows:
        e = (r["email"] or "").strip().lower()
        if e:
            roster_by_email[e] = dict(r)
        if r["sis_id"]:
            roster_sis_ids.add(str(r["sis_id"]))
    logger.info(
        f"reconcile: roster={len(roster_by_email)} emails, "
        f"{len(roster_sis_ids)} SIS ids, dry_run={dry_run}"
    )

    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    # ── 4. DIRECTION A — active-in-roster, verify Google side ─────────
    dir_a_applied = 0
    async with AsyncSessionLocal() as db:
        g = GoogleWorkspaceAdapter(db)
        svc = await g._get_service()
        batch = 30
        emails = list(roster_by_email.keys())
        result["dir_a_scanned"] = len(emails)
        for i in range(0, len(emails), batch):
            slice_emails = emails[i:i + batch]
            for email in slice_emails:
                rr = roster_by_email[email]
                target_ou = ou_map.get(rr["school"])

                # Non-district email in SIS → record for operator review,
                # don't call Google (would 403). Tim's rule: kid is still
                # attached to the district in the SIS, they just have the
                # wrong email on their SIS record.
                email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
                if district_domains and email_domain not in district_domains:
                    await _record_candidate(
                        db, run_id=run_id, direction="A",
                        email=email, sis_id=rr.get("sis_id"),
                        name=f"{rr['first_name']} {rr['last_name']}",
                        roster_school=rr.get("school"),
                        google_ou=None, target_ou=None,
                        decision="non_district_email",
                        reason=(f"SIS email domain '{email_domain}' is not "
                                f"in the district allowlist. Update the SIS "
                                f"record to a district-domain email."),
                        last_login=None, google_creation=None,
                    )
                    result["dir_a_candidates"] += 1
                    continue

                try:
                    u = await g.get_user(email)
                except Exception as e:
                    result["errors"].append(f"A/{email}: get_user: {e}")
                    continue

                decision = None
                reason = None
                if not u:
                    decision = "missing_from_google"
                    reason = "Active in roster but no Google account exists"
                else:
                    current_ou = u.get("org_unit_path") or ""
                    is_susp = bool(u.get("suspended"))
                    if is_susp or current_ou.startswith("/Archived"):
                        decision = "reactivate_and_move"
                        reason = (
                            f"Roster=active, Google={'suspended' if is_susp else 'ok'}"
                            f", ou={current_ou or '?'}, want={target_ou}"
                        )
                    elif target_ou and current_ou != target_ou:
                        decision = "move_ou"
                        reason = f"OU mismatch: {current_ou} → {target_ou}"
                    else:
                        continue  # already OK

                # Record candidate
                await _record_candidate(
                    db, run_id=run_id, direction="A",
                    email=email, sis_id=rr.get("sis_id"),
                    name=f"{rr['first_name']} {rr['last_name']}",
                    roster_school=rr.get("school"),
                    google_ou=(u.get("org_unit_path") if u else None),
                    target_ou=target_ou,
                    decision=decision, reason=reason,
                    last_login=_parse_gts(u.get("last_login") if u else None),
                    google_creation=None,
                )
                result["dir_a_candidates"] += 1

                # Apply
                if not dry_run and decision in ("reactivate_and_move", "move_ou"):
                    try:
                        if decision == "reactivate_and_move" and u.get("suspended"):
                            rr_res = await g.reactivate_account(email)
                            if not rr_res.success:
                                raise RuntimeError(f"reactivate: {rr_res.error}")
                        if target_ou and (u.get("org_unit_path") or "") != target_ou:
                            mv = await g.move_user_ou(email, target_ou)
                            if not mv.success:
                                raise RuntimeError(f"move_ou: {mv.error}")
                        # Keep the roster's cached google_ou in sync with the
                        # move we just made — otherwise the directory chip
                        # stays stale until the next SIS import overwrites it.
                        if target_ou:
                            await db.execute(text(
                                "UPDATE roster_snapshots SET google_ou = :ou "
                                "WHERE lower(email) = :e"
                            ).bindparams(ou=target_ou, e=email))
                        await _mark_applied(db, run_id, email, direction="A",
                                            error=None)
                        await _audit_apply(
                            db, action="roster.reconcile.dir_a.apply",
                            email=email, name=f"{rr['first_name']} {rr['last_name']}",
                            details={"decision": decision, "reason": reason,
                                     "target_ou": target_ou},
                        )
                        dir_a_applied += 1
                    except Exception as e:
                        result["errors"].append(f"A/{email}: apply: {e}")
                        await _mark_applied(db, run_id, email, direction="A",
                                            error=str(e)[:400])
            await db.commit()
        result["dir_a_applied"] = dir_a_applied

    # ── 5. DIRECTION B — active-in-Google, verify roster side ─────────
    # Iterate each student OU and pull raw user data (need externalIds for
    # SIS-id matching, which list_users doesn't return).
    now = datetime.now(timezone.utc)
    lastlogin_floor = now.timestamp() - lastlogin_min_days * 86400

    def _list_full(ou_path: str) -> list[dict]:
        out: list[dict] = []
        req = svc.users().list(
            customer="my_customer",
            query=f"orgUnitPath='{ou_path}'",
            maxResults=500, projection="full", orderBy="email",
        )
        while req:
            resp = req.execute()
            out.extend(resp.get("users", []))
            req = svc.users().list_next(req, resp)
        return out

    dir_b_applied = 0
    async with AsyncSessionLocal() as db:
        g = GoogleWorkspaceAdapter(db)
        svc = await g._get_service()

        candidates_to_apply: list[dict] = []
        for school_code, ou_path in ou_map.items():
            try:
                raw_users = await asyncio.to_thread(_list_full, ou_path)
            except Exception as e:
                result["errors"].append(f"B/list {ou_path}: {e}")
                continue

            for u in raw_users:
                if u.get("suspended"):
                    continue  # already suspended — not a candidate
                email = (u.get("primaryEmail") or "").lower()
                if not email:
                    continue
                result["dir_b_scanned"] += 1

                # Match: roster by email OR by SIS id from externalIds
                sis_ids_in_google = [
                    str(x.get("value")) for x in (u.get("externalIds") or [])
                    if x.get("type") == "organization" and x.get("value")
                ]
                in_roster = email in roster_by_email or any(
                    sid in roster_sis_ids for sid in sis_ids_in_google
                )
                if in_roster:
                    continue

                local_part = email.split("@", 1)[0]

                # Explicit operator ignore list (shared library / office /
                # test accounts that live in a student OU).
                if email in ignore_emails:
                    await _record_candidate(
                        db, run_id=run_id, direction="B",
                        email=email,
                        sis_id=sis_ids_in_google[0] if sis_ids_in_google else None,
                        name=u.get("name", {}).get("fullName"),
                        roster_school=school_code,
                        google_ou=u.get("orgUnitPath"),
                        target_ou=None,
                        decision="ignored_shared_account",
                        reason="Email is on reconcile_ignore_emails allowlist.",
                        last_login=_parse_gts(u.get("lastLoginTime")),
                        google_creation=_parse_gts(u.get("creationTime")),
                    )
                    result["dir_b_candidates"] += 1
                    continue

                # Name-shape heuristic — student emails always end in a
                # 2- or 3-digit grad-year suffix. `peslibrary`, `testphs`,
                # `poffice`, etc. don't match. Never archive these.
                if not student_local_re.search(local_part):
                    await _record_candidate(
                        db, run_id=run_id, direction="B",
                        email=email,
                        sis_id=sis_ids_in_google[0] if sis_ids_in_google else None,
                        name=u.get("name", {}).get("fullName"),
                        roster_school=school_code,
                        google_ou=u.get("orgUnitPath"),
                        target_ou=None,
                        decision="non_student_shared_account",
                        reason=(f"Local part '{local_part}' has no grad-year "
                                f"suffix — not a student naming pattern. "
                                f"Consider moving out of the student OU."),
                        last_login=_parse_gts(u.get("lastLoginTime")),
                        google_creation=_parse_gts(u.get("creationTime")),
                    )
                    result["dir_b_candidates"] += 1
                    continue

                # Grad-year sanity check — the trailing 2-digit suffix on
                # a student email encodes their graduation year (last two
                # digits). If it hasn't happened yet, the kid should
                # still be enrolled — refusing to archive protects
                # against the "current student mistakenly flagged"
                # class of bug (see hallm35 = 2035 grad, incident
                # 2026-09-08). Assumes 20xx school-year window.
                #
                # Reserved SpEd suffix codes: some district codes look
                # like grad years but aren't. "23" is a SpEd
                # designation, not the class of 2023 — kids with a
                # trailing "23" are current students on an extended
                # program. Configurable via
                # roster.reconcile_reserved_suffix_codes (comma-separated,
                # default "23"). Any hit → skip regardless of what
                # 20xx would compute.
                _grad_m = _re.search(r"(\d{2})$", local_part)
                if _grad_m:
                    suffix_2d = _grad_m.group(1)
                    reserved_raw = await _gsv(db, "roster", "reconcile_reserved_suffix_codes") or "23"
                    reserved_set = {s.strip() for s in reserved_raw.split(",") if s.strip()}
                    if suffix_2d in reserved_set:
                        await _record_candidate(
                            db, run_id=run_id, direction="B",
                            email=email,
                            sis_id=sis_ids_in_google[0] if sis_ids_in_google else None,
                            name=u.get("name", {}).get("fullName"),
                            roster_school=school_code,
                            google_ou=u.get("orgUnitPath"),
                            target_ou=None,
                            decision="skip_reserved_suffix_code",
                            reason=(f"Email suffix '{suffix_2d}' is on the "
                                    f"reserved-codes list (SpEd / extended "
                                    f"program). Not a graduation year — "
                                    f"skipping archive."),
                            last_login=_parse_gts(u.get("lastLoginTime")),
                            google_creation=_parse_gts(u.get("creationTime")),
                        )
                        result["dir_b_candidates"] += 1
                        continue
                    try:
                        grad_year = 2000 + int(suffix_2d)
                        current_year = now.year
                        if grad_year >= current_year:
                            await _record_candidate(
                                db, run_id=run_id, direction="B",
                                email=email,
                                sis_id=sis_ids_in_google[0] if sis_ids_in_google else None,
                                name=u.get("name", {}).get("fullName"),
                                roster_school=school_code,
                                google_ou=u.get("orgUnitPath"),
                                target_ou=None,
                                decision="skip_future_grad_year",
                                reason=(f"Grad year {grad_year} >= current "
                                        f"year {current_year} — student should "
                                        f"still be enrolled; refusing to "
                                        f"archive on missing-roster signal."),
                                last_login=_parse_gts(u.get("lastLoginTime")),
                                google_creation=_parse_gts(u.get("creationTime")),
                            )
                            result["dir_b_candidates"] += 1
                            continue
                    except (ValueError, TypeError):
                        pass

                # Newly-created account grace period — guidance sometimes
                # takes weeks to assign classes for a new enrollee, and
                # brand-new accounts have zero login history. Skip any
                # account younger than `reconcile_new_account_grace_days`
                # (default 45). Read from settings so ops can adjust.
                new_grace_days = 45
                try:
                    _g = await _gsv(db, "roster", "reconcile_new_account_grace_days")
                    if _g:
                        new_grace_days = int(_g)
                except (ValueError, TypeError):
                    pass
                creation_dt = _parse_gts(u.get("creationTime"))
                if creation_dt and (now - creation_dt).days < new_grace_days:
                    age_days = (now - creation_dt).days
                    await _record_candidate(
                        db, run_id=run_id, direction="B",
                        email=email,
                        sis_id=sis_ids_in_google[0] if sis_ids_in_google else None,
                        name=u.get("name", {}).get("fullName"),
                        roster_school=school_code,
                        google_ou=u.get("orgUnitPath"),
                        target_ou=None,
                        decision="skip_new_account_grace",
                        reason=(f"Account created {age_days}d ago "
                                f"(< {new_grace_days}d grace) — new-enrollee "
                                f"paperwork may still be in flight."),
                        last_login=_parse_gts(u.get("lastLoginTime")),
                        google_creation=creation_dt,
                    )
                    result["dir_b_candidates"] += 1
                    continue

                last_login_raw = u.get("lastLoginTime")
                last_login_dt = _parse_gts(last_login_raw)
                # Guard: unknown last_login → SKIP, don't archive.
                # Google returns "1970-01-01T00:00:00.000Z" (epoch) or
                # None when lastLoginTime isn't populated (fresh accounts,
                # some permissions edge cases, occasional stale field).
                # Old logic treated epoch as "way past floor" → archived
                # active students whose accounts merely lacked a
                # lastLoginTime. Real-world hit: hallm35 (Makenzie Hall,
                # SIS_A grade 4) logged in Sept 2 but Google returned
                # epoch, and her roster_snapshots.email was blank so
                # neither matching path found her either. Result: falsely
                # archived on 2026-09-08 (reverted). Treat unknown as
                # "not enough data to archive" and log it as a candidate
                # that needs manual review instead.
                if last_login_dt is None or last_login_dt.year < 2000:
                    await _record_candidate(
                        db, run_id=run_id, direction="B",
                        email=email,
                        sis_id=sis_ids_in_google[0] if sis_ids_in_google else None,
                        name=u.get("name", {}).get("fullName"),
                        roster_school=school_code,
                        google_ou=u.get("orgUnitPath"),
                        target_ou=None,
                        decision="skip_unknown_lastlogin",
                        reason=(f"lastLoginTime is {last_login_raw or 'null'} "
                                f"(unknown / epoch). Refusing to archive on "
                                f"insufficient signal; needs manual review."),
                        last_login=None,
                        google_creation=_parse_gts(u.get("creationTime")),
                    )
                    result["dir_b_candidates"] += 1
                    continue

                last_login_ts = last_login_dt.timestamp()
                # lastlogin floor: recent activity → skip
                if last_login_ts > lastlogin_floor:
                    await _record_candidate(
                        db, run_id=run_id, direction="B",
                        email=email,
                        sis_id=sis_ids_in_google[0] if sis_ids_in_google else None,
                        name=u.get("name", {}).get("fullName"),
                        roster_school=school_code,
                        google_ou=u.get("orgUnitPath"),
                        target_ou=None,
                        decision="skip_recent_login",
                        reason=f"Last login {last_login_raw} within "
                               f"{lastlogin_min_days}d floor",
                        last_login=last_login_dt,
                        google_creation=_parse_gts(u.get("creationTime")),
                    )
                    result["dir_b_candidates"] += 1
                    continue

                # Real archive candidate
                candidates_to_apply.append({
                    "email": email,
                    "sis_id": sis_ids_in_google[0] if sis_ids_in_google else None,
                    "name": u.get("name", {}).get("fullName"),
                    "school": school_code,
                    "current_ou": u.get("orgUnitPath"),
                    "last_login_raw": last_login_raw,
                    "last_login_dt": last_login_dt,
                    "google_creation": _parse_gts(u.get("creationTime")),
                })

        # Cap: if more than max_per_run, act on the OLDEST-login first
        # (safer targets), queue the rest for review.
        candidates_to_apply.sort(key=lambda c: (
            c["last_login_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc)
        ))
        act_slice = candidates_to_apply[:max_per_run]
        queue_slice = candidates_to_apply[max_per_run:]

        for c in queue_slice:
            await _record_candidate(
                db, run_id=run_id, direction="B",
                email=c["email"], sis_id=c["sis_id"], name=c["name"],
                roster_school=c["school"], google_ou=c["current_ou"],
                target_ou=deprov_ou, decision="queue_max_per_run_cap",
                reason=(f"Would archive, but per-run cap of {max_per_run} "
                        f"reached. Bump reconcile_max_per_run to process."),
                last_login=c["last_login_dt"],
                google_creation=c["google_creation"],
            )
            result["dir_b_candidates"] += 1

        for c in act_slice:
            decision = "archive_stale"
            reason = (
                f"No roster row, last login {c['last_login_raw'] or 'never'} "
                f"(>{lastlogin_min_days}d floor)"
            )
            await _record_candidate(
                db, run_id=run_id, direction="B",
                email=c["email"], sis_id=c["sis_id"], name=c["name"],
                roster_school=c["school"], google_ou=c["current_ou"],
                target_ou=deprov_ou, decision=decision, reason=reason,
                last_login=c["last_login_dt"],
                google_creation=c["google_creation"],
            )
            result["dir_b_candidates"] += 1

            if not dry_run:
                try:
                    if (c["current_ou"] or "") != deprov_ou:
                        mv = await g.move_user_ou(c["email"], deprov_ou)
                        if not mv.success:
                            raise RuntimeError(f"move_ou: {mv.error}")
                    sp = await g.suspend_account(c["email"])
                    if not sp.success:
                        raise RuntimeError(f"suspend: {sp.error}")
                    await _mark_applied(db, run_id, c["email"],
                                        direction="B", error=None)
                    await _audit_apply(
                        db, action="roster.reconcile.dir_b.apply",
                        email=c["email"], name=c["name"],
                        details={
                            "decision": decision, "reason": reason,
                            "from_ou": c["current_ou"], "to_ou": deprov_ou,
                            "last_login": c["last_login_raw"],
                            "sis_id": c["sis_id"],
                        },
                    )
                    dir_b_applied += 1
                except Exception as e:
                    result["errors"].append(f"B/{c['email']}: apply: {e}")
                    await _mark_applied(db, run_id, c["email"],
                                        direction="B", error=str(e)[:400])

        await db.commit()
        result["dir_b_applied"] = dir_b_applied

    # ── 6. Finish run row ─────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        await db.execute(text("""
            UPDATE student_reconcile_runs SET
              finished_at = NOW(),
              dir_a_scanned=:aa, dir_a_candidates=:ab, dir_a_applied=:ac,
              dir_b_scanned=:ba, dir_b_candidates=:bb, dir_b_applied=:bc,
              errors=:err,
              notes=CAST(:notes AS JSONB)
            WHERE id=:id
        """).bindparams(
            id=run_id,
            aa=result["dir_a_scanned"], ab=result["dir_a_candidates"], ac=dir_a_applied,
            ba=result["dir_b_scanned"], bb=result["dir_b_candidates"], bc=dir_b_applied,
            err=len(result["errors"]),
            notes=json.dumps({"errors_sample": result["errors"][:20],
                              "settings": {
                                  "stale_days": stale_days,
                                  "lastlogin_min_days": lastlogin_min_days,
                                  "max_per_run": max_per_run,
                                  "min_roster_active": min_roster_active,
                              }}),
        ))
        await db.commit()

    logger.info(
        f"reconcile finished run_id={run_id} mode={result['mode']} "
        f"A: {result['dir_a_candidates']}/{result['dir_a_scanned']} "
        f"applied={dir_a_applied}   "
        f"B: {result['dir_b_candidates']}/{result['dir_b_scanned']} "
        f"applied={dir_b_applied}   errors={len(result['errors'])}"
    )
    return result


# ── helpers ───────────────────────────────────────────────────────────

def _parse_gts(gts: str | None) -> datetime | None:
    """Parse a Google API timestamp ('2026-08-25T17:59:00.000Z') into a
    tz-aware datetime, or None. Treats the 1970-epoch placeholder
    ('never logged in') as None."""
    if not gts:
        return None
    try:
        dt = datetime.fromisoformat(gts.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.year <= 1971:
        return None
    return dt


async def _record_run_row(db, *, mode, aborted_reason, roster_active_count,
                          counts):
    result = await db.execute(text("""
        INSERT INTO student_reconcile_runs
          (mode, aborted_reason, roster_active_count)
        VALUES (:m, :a, :r)
        RETURNING id
    """).bindparams(m=mode, a=aborted_reason, r=roster_active_count))
    return int(result.scalar_one())


async def _record_candidate(db, *, run_id, direction, email, sis_id, name,
                            roster_school, google_ou, target_ou, decision,
                            reason, last_login, google_creation):
    await db.execute(text("""
        INSERT INTO student_reconcile_candidates
          (run_id, direction, email, sis_id, name, roster_school,
           google_ou, target_ou, decision, reason, last_login, google_creation)
        VALUES
          (:run_id, :direction, :email, :sis_id, :name, :school,
           :gou, :tou, :dec, :reason, :ll, :gc)
    """).bindparams(
        run_id=run_id, direction=direction, email=email,
        sis_id=sis_id, name=name, school=roster_school,
        gou=google_ou, tou=target_ou, dec=decision, reason=reason,
        ll=last_login, gc=google_creation,
    ))


async def _mark_applied(db, run_id, email, *, direction, error):
    await db.execute(text("""
        UPDATE student_reconcile_candidates
        SET applied = :ok,
            applied_at = NOW(),
            apply_error = :err
        WHERE run_id = :run_id AND direction = :d AND email = :email
    """).bindparams(
        ok=(error is None), err=error, run_id=run_id, d=direction, email=email,
    ))


async def _audit_apply(db, *, action, email, name, details):
    from app.audit.service import log_action
    await log_action(
        db, actor="system", action=action, module="roster", target=email,
        details=json.dumps({"name": name, **details}),
    )
