"""
Staff directory sync job — pulls user list from Google Workspace.

Google is the source of truth for staff identity.
Runs every 15 minutes via the scheduler.
"""

import difflib
import json as _json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select, delete, text, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# Default non-person filters. Districts can extend these via the
# google.non_person_patterns setting (one regex per line). The patterns
# match against the lowercased email address. Anything that matches is
# excluded from staff_directory so it never enters the deprovision
# queue or any downstream display.
_DEFAULT_NON_PERSON_PATTERNS = [
    # Generic device / shared accounts
    r"\.ipad@",
    r"\.cast@",
    r"\.kiosk@",
    r"\.printer@",
    # Function / role / department mailboxes — match the local part
    # ending in one of these words. Catches `phslibrary@`, `phsspecial@`,
    # `phs-pjhssafetyteam@`, etc.
    r"(safety|library|special|team|reception|info|cast|ipad|kiosk|printer|committee|group|board|guidance)@",
    # Lifecycle suffixes — accounts marked as archive, legacy, or old
    r"[_.\-]archive@",
    r"[_.\-]legacy@",
    r"[_.\-]old@",
    # Year suffixes used for graduation cohorts (rare for staff)
    r"^(pbis|class|cohort)[-_]?\d{2,4}@",
    # Vendor / partner / external relationships under the district domain
    r"^(southernstate|ohiochristian|shawneestate|ssu)@",
]


def _compile_non_person_patterns(custom_patterns: list[str]) -> list[re.Pattern]:
    """Combine defaults with district custom patterns and compile."""
    out = []
    for p in _DEFAULT_NON_PERSON_PATTERNS + (custom_patterns or []):
        p = (p or "").strip()
        if not p:
            continue
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.warning(f"non_person_patterns: invalid regex {p!r}: {e}")
    return out


def _is_non_person(email: str, patterns: list[re.Pattern]) -> bool:
    """True if the email matches any non-person pattern."""
    if not email:
        return False
    em = email.lower()
    return any(p.search(em) for p in patterns)


async def sync_staff_directory(ctx: dict) -> dict:
    """
    Pull all users from Google Workspace, refresh the staff_directory
    cache, compute each row's match_state, and queue auto-deprovisions
    for unmatched accounts.

    Match logic (per Google staff account, in order):
        1. If staff_ignores has kind='non_person' for this email → drop
           the account entirely (never enters staff_directory).
        2. If staff_ignores has kind='confirmed' → match_state='override'.
        3. If matches HR (email or nickname-aware name) → 'hr_match'.
        4. If matches any building room roster → 'roster_match'.
        5. Otherwise → 'unmatched'.

    Anything that lands in 'unmatched' and doesn't already have a
    pending queue entry gets a fresh deprovision queue row inserted
    with source='auto_unmatched'.

    Returns {"synced": N, "by_state": {...}, ...} for ARQ visibility.
    """
    from app.db.engine import AsyncSessionLocal
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.modules.staff.models import (
        StaffDirectoryEntry, StaffIgnore, HRStaffCache,
    )
    from app.modules.staff.nicknames import get_nickname_variants
    from app.workers.hr_diff_job import (
        _load_room_roster_names, _matches_room_roster, canon_name_part,
    )

    result = {
        "job": "sync_staff_directory",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "synced": 0,
        "error": None,
    }

    try:
        async with AsyncSessionLocal() as db:
            adapter = GoogleWorkspaceAdapter(db)

            # ── Load Settings ─────────────────────────────────────────
            from app.modules.settings.repository import get_setting_value
            ou_staff_raw = await get_setting_value(db, "google", "ou_staff") or ""
            if ou_staff_raw:
                staff_ou_paths = [p.strip() for p in ou_staff_raw.split(",") if p.strip()]
            else:
                staff_ou_paths = ["/Users-Teachers", "/Users-Admin-Staff", "/Users-Classified-Staff", "/Users-Tech-Admin"]

            # Walk-all-OUs mode (default). Rather than issue N queries
            # for each configured staff OU, sweep the whole domain and
            # filter out student paths post-hoc. Catches staff accounts
            # sitting in atypical OUs — root ('/'), '/Archived Accounts'
            # for rehires — that the explicit OU list would miss and
            # that surface today as "phantom new hires" on the roster
            # provision queue.
            #
            # Setting `google.staff_sync_walk_all` = "false" reverts to
            # the explicit-list behavior. Off by default to preserve
            # existing tuning knob.
            walk_all_raw = (await get_setting_value(db, "google", "staff_sync_walk_all") or "true").strip().lower()
            walk_all = walk_all_raw in ("1", "true", "yes", "on")
            if walk_all:
                # Single query pulls every account in the domain; post-
                # filter below drops student OU paths. Explicit ou_staff
                # still drives per-OU building assignment.
                STAFF_OUS = [""]
            else:
                STAFF_OUS = [f"orgUnitPath='{ou}'" for ou in staff_ou_paths]

            ou_map_raw = await get_setting_value(db, "google", "ou_building_map") or "{}"
            try:
                ou_building_map = _json.loads(ou_map_raw)
            except Exception:
                ou_building_map = {}

            # ── Compile non-person filters ─────────────────────────────
            custom_raw = await get_setting_value(db, "google", "non_person_patterns") or ""
            custom_patterns = [
                line.strip() for line in custom_raw.replace(",", "\n").splitlines()
                if line.strip()
            ]
            non_person_filters = _compile_non_person_patterns(custom_patterns)

            # ── Load active staff_ignores (the manual override table) ──
            ignore_rows = (await db.execute(
                select(StaffIgnore).where(StaffIgnore.restored_at.is_(None))
            )).scalars().all()
            ignore_by_email: dict[str, str] = {
                (i.username or "").lower().strip(): i.kind
                for i in ignore_rows
                if i.username
            }

            # ── Load HR cache for confirmation matching ────────────────
            # Two sets:
            #   hr_name_set        — "first last" with original punctuation
            #   hr_name_set_canon  — "first <canonlast>" with last name
            #                        stripped of non-alphanumerics; used
            #                        as a fallback so "Libby Stonge" in
            #                        HR matches "Libby St Onge" in Google
            #                        and "Libby St. Onge" in rosters.
            hr_rows = (await db.execute(select(HRStaffCache))).scalars().all()
            hr_emails: set[str] = set()
            hr_name_set: set[str] = set()
            hr_name_set_canon: set[str] = set()
            # Per-key lookup back to the HRStaffCache row so the main
            # directory loop can copy notes + derive LOA when a match
            # fires. Keyed by the same strings that populate hr_emails
            # / hr_name_set so the lookup is zero-cost during matching.
            hr_info_by_email: dict[str, "HRStaffCache"] = {}
            hr_info_by_name: dict[str, "HRStaffCache"] = {}
            hr_info_by_name_canon: dict[str, "HRStaffCache"] = {}
            # Strip suffixes ("Jr.", "III", etc.) from HR last names and
            # expand hyphenated/compound last names so half-name Google
            # accounts ("Shay White" vs HR's "Shay Pennington-White")
            # still match.
            from app.workers.hr_sync_job import (
                _strip_suffix,
                _last_name_variants,
                is_on_leave as _is_on_leave_hr,
                parse_former_last_name as _parse_former_last_name,
            )
            for h in hr_rows:
                he = _strip_suffix((h.email or "").lower().strip())
                if he:
                    hr_emails.add(he)
                    hr_info_by_email.setdefault(he, h)
                name = (h.name or "").strip()
                parts = name.split(None, 1)
                if len(parts) >= 2:
                    fn = parts[0].lower()
                    ln_raw = _strip_suffix(parts[1].lower())
                    ln_variants = _last_name_variants(ln_raw)
                    # Former / maiden surname from HR Notes — lets a
                    # Google "jennifer.buffington" account resolve to
                    # HR's current "Jennifer Carver Née: Buffington".
                    former_last = _parse_former_last_name(h.notes)
                    if former_last:
                        ln_variants = list(
                            set(ln_variants) | set(_last_name_variants(former_last.lower()))
                        )
                    first_variants = [fn] + list(get_nickname_variants(fn))
                    for ln in ln_variants:
                        for fv in first_variants:
                            key = f"{fv} {ln}"
                            hr_name_set.add(key)
                            hr_info_by_name.setdefault(key, h)
                            canon_key = f"{fv} {canon_name_part(ln)}"
                            hr_name_set_canon.add(canon_key)
                            hr_info_by_name_canon.setdefault(canon_key, h)
            hr_name_list = list(hr_name_set)

            # ── Load room roster cache for confirmation matching ──────
            roster_names = await _load_room_roster_names(db)

            # ── Pull users from Google ────────────────────────────────
            users = []
            seen_emails: set[str] = set()
            failed_ous: list[str] = []
            # Student-OU prefixes get dropped BEFORE any per-user logic.
            # Kept as a hardcoded pair — student layout is district-wide
            # stable, and putting this in config invites drift where a
            # missed setting causes 3000 students to appear on the
            # staff directory page.
            _STUDENT_OU_PREFIXES = ("/Users-Students/", "/Archived Accounts/Students")

            # Student-email pattern — matches this district's SIS-issued
            # local-part shape: last-name-word + initial + digits (e.g.
            # "adamsa37@"). Real staff emails are always "first.last@"
            # so the two are unambiguous in this domain. Used to filter
            # archived-but-not-under-/Archived Accounts/Students student
            # rows that would otherwise flood staff_directory (~2500
            # rows observed 2026-08-06).
            import re as _re
            _STUDENT_EMAIL_RE = _re.compile(r"^[a-z]{3,}[a-z][0-9]+@")

            for ou_query in STAFF_OUS:
                try:
                    # Pull suspended accounts too — people on leave-of-
                    # absence keep their Google account in a suspended
                    # state, and HR still lists them. If we exclude
                    # suspended here, the HR diff treats them as missing
                    # and queues bogus provision requests. They're
                    # included in staff_directory with status='suspended'
                    # and filtered out of the auto-deprov loop below so
                    # they don't clog the deprov queue either.
                    #
                    # max_results=20000 is a safety ceiling well above
                    # the district's ~10k total accounts — pagination
                    # inside list_users stops on nextPageToken=None, not
                    # on this limit, so a domain that grows still gets
                    # a full sweep.
                    ou_users = await adapter.list_users(
                        query=ou_query, max_results=20000,
                    )
                    for u in ou_users:
                        em = (u.get("email") or "").lower()
                        if not em or em in seen_emails:
                            continue
                        # Drop student accounts from the walk-all sweep.
                        # No-op when ou_query already scoped to a staff
                        # OU (student prefixes won't be in results).
                        ou_path = (u.get("org_unit") or "").rstrip("/") + "/"
                        if any(ou_path.startswith(p) for p in _STUDENT_OU_PREFIXES):
                            continue
                        # Also drop by email pattern — students archived
                        # to a bare /Archived Accounts (no /Students
                        # suffix) escape the OU check but match the SIS
                        # local-part shape (lastnameX##).
                        if _STUDENT_EMAIL_RE.match(em):
                            continue

                        # Manual and regex non_person flags kept separate.
                        # The manual flag must win over HR/roster below
                        # (otherwise re-adding an ignored ex-staffer to
                        # HR would revert them to hr_match); the regex
                        # flag only fires when no authoritative source
                        # claims the account. Both were previously
                        # sharing a single flag which conflated the
                        # priority — and the manual branch dropped rows
                        # entirely, so admins couldn't see whether
                        # their ignore action took effect.
                        u["_manual_non_person"] = ignore_by_email.get(em) == "non_person"
                        u["_regex_non_person"] = _is_non_person(em, non_person_filters)
                        seen_emails.add(em)
                        users.append(u)
                except Exception as e:
                    logger.error(
                        f"Staff sync: failed to query {ou_query}: {e}"
                    )
                    failed_ous.append(ou_query)

            # ── ABORT-ON-FAILURE GUARD ─────────────────────────────
            # If ANY OU query failed, refuse to rebuild the directory.
            # Google returning a transient 500 for one OU while others
            # succeed would otherwise "deprovision" every user in the
            # missing OU (dropped from staff_directory → looked absent
            # to reconciliation → auto-queued deprovisioning + room-
            # roster auto-queue false-positive provisioning). Preserve
            # the previous good state and surface the error loudly.
            if failed_ous:
                result["error"] = (
                    f"Staff sync ABORTED: {len(failed_ous)} OU quer"
                    f"{'y' if len(failed_ous) == 1 else 'ies'} failed: "
                    f"{', '.join(failed_ous)}. Directory NOT rewritten "
                    f"— previous state preserved."
                )
                result["failed_ous"] = failed_ous
                logger.error(result["error"])
                try:
                    from app.audit.service import log_action
                    await log_action(
                        db, actor="system",
                        action="staff.sync.aborted_partial_fetch",
                        module="staff",
                        target=f"{len(failed_ous)} OU(s) failed",
                        details=result["error"][:500],
                    )
                    await db.commit()
                except Exception:
                    pass
                return result

            # ── DIRECTORY-DIFF SANITY GATE ─────────────────────────
            # Mirrors hr_diff_job's gate. The abort-on-failure guard
            # above catches "one OU query blew up," but Google can
            # also return 200 with a suspiciously small user list
            # (transient index issue, quota throttling, revoked scope
            # partial). In that case failed_ous is empty but `users`
            # is dangerously light — a rebuild would drop hundreds of
            # real staff from staff_directory, which downstream
            # deprovision auto-queues would then propose for removal.
            # Prior incident: 2026-06-13 216-row queue misfire tied
            # to a mid-refresh empty directory. Refuse to rewrite when:
            #   1. incoming user set is below an absolute floor, OR
            #   2. incoming user set shrank more than 25% vs the
            #      currently-cached directory count.
            # Both bounds are conservative for a district of ~1000
            # staff accounts and can be tuned via settings later if
            # they misfire.
            _MIN_USERS_ABSOLUTE = 200
            _MAX_SHRINK = 0.25
            incoming_count = len(users)
            current_count = (await db.execute(
                select(func.count()).select_from(StaffDirectoryEntry)
            )).scalar_one()
            if incoming_count < _MIN_USERS_ABSOLUTE:
                msg = (
                    f"Staff sync ABORTED: Google returned only {incoming_count} "
                    f"users (< {_MIN_USERS_ABSOLUTE} floor). Directory NOT "
                    f"rewritten — previous {current_count} rows preserved. "
                    f"Suspect transient Google outage / permission scope "
                    f"revocation / OU-empty state."
                )
                logger.error(msg)
                result["error"] = msg
                try:
                    from app.audit.service import log_action
                    await log_action(
                        db, actor="system",
                        action="staff.sync.aborted_low_result_count",
                        module="staff",
                        target=f"incoming={incoming_count} current={current_count}",
                        details=msg[:500],
                    )
                    await db.commit()
                except Exception:
                    pass
                return result
            if current_count and incoming_count < current_count * (1 - _MAX_SHRINK):
                msg = (
                    f"Staff sync ABORTED: Google returned {incoming_count} users; "
                    f"directory currently has {current_count} ({incoming_count/current_count:.1%} "
                    f"— shrinkage >{_MAX_SHRINK:.0%}). Directory NOT rewritten — "
                    f"previous state preserved. Confirm Google is healthy and re-run manually."
                )
                logger.error(msg)
                result["error"] = msg
                try:
                    from app.audit.service import log_action
                    await log_action(
                        db, actor="system",
                        action="staff.sync.aborted_high_shrinkage",
                        module="staff",
                        target=f"incoming={incoming_count} current={current_count}",
                        details=msg[:500],
                    )
                    await db.commit()
                except Exception:
                    pass
                return result

            # ── Compute match_state per surviving user ────────────────
            from app.integrations.google.adapter import _parse_building_from_ou
            from app.modules.settings.buildings import get_building_maps, resolve_building_code_sync

            # Load building resolver maps once for the tight loop below.
            building_maps = await get_building_maps(db)

            by_state: dict[str, int] = {
                "hr_match": 0, "roster_match": 0, "override": 0,
                "non_person": 0, "unmatched": 0,
            }

            # Clear and rebuild cache
            await db.execute(delete(StaffDirectoryEntry))

            unmatched_users: list[dict] = []
            for u in users:
                em = (u.get("email") or "").lower()
                first = (u.get("first_name") or "").strip()
                last = (u.get("last_name") or "").strip()
                first_l = first.lower()
                last_l = last.lower()
                # Parse from OU first (the fast path), then run through
                # the resolver as a safety net so the building field is
                # ALWAYS a canonical internal code in staff_directory.
                building = _parse_building_from_ou(
                    u.get("org_unit", ""), ou_building_map
                ) or u.get("building")
                canon = resolve_building_code_sync(building, building_maps)
                if canon:
                    building = canon

                # Aliases (maiden names, legacy addresses) — match each
                # alias email against HR as well as the primary.
                aliases = [a.lower() for a in (u.get("aliases") or []) if a]

                # Canonicalized last name for punctuation-tolerant match
                last_canon = canon_name_part(last_l)
                first_first_word = first_l.split()[0] if first_l else ""

                # Confirmation order: manual override → manual non_person
                # → HR → roster → regex non_person → unmatched.
                # Manual non_person MUST win over HR — if a non-staff
                # account had an HR row lingering (e.g. board member on
                # HR sheet), HR would otherwise claim it and revert
                # the ignore. The manual branch used to drop the
                # account entirely; now the row enters staff_directory
                # with match_state='non_person' so admins can verify
                # their ignore worked (visible under the non_person
                # filter alongside regex-flagged rows).
                hr_match_row = None
                if ignore_by_email.get(em) == "confirmed":
                    state = "override"
                elif u.get("_manual_non_person"):
                    state = "non_person"
                elif em in hr_emails:
                    state = "hr_match"
                    hr_match_row = hr_info_by_email.get(em)
                elif any(a in hr_emails for a in aliases):
                    state = "hr_match"
                    for a in aliases:
                        if a in hr_info_by_email:
                            hr_match_row = hr_info_by_email[a]
                            break
                elif f"{first_l} {last_l}" in hr_name_set:
                    state = "hr_match"
                    hr_match_row = hr_info_by_name.get(f"{first_l} {last_l}")
                elif any(
                    f"{v} {last_l}" in hr_name_set
                    for v in get_nickname_variants(first_l)
                ):
                    state = "hr_match"
                    for v in get_nickname_variants(first_l):
                        key = f"{v} {last_l}"
                        if key in hr_info_by_name:
                            hr_match_row = hr_info_by_name[key]
                            break
                # Canonical fallback — "Libby St Onge" → "libby stonge"
                elif f"{first_first_word} {last_canon}" in hr_name_set_canon:
                    state = "hr_match"
                    hr_match_row = hr_info_by_name_canon.get(f"{first_first_word} {last_canon}")
                elif any(
                    f"{v} {last_canon}" in hr_name_set_canon
                    for v in get_nickname_variants(first_first_word)
                ):
                    state = "hr_match"
                    for v in get_nickname_variants(first_first_word):
                        key = f"{v} {last_canon}"
                        if key in hr_info_by_name_canon:
                            hr_match_row = hr_info_by_name_canon[key]
                            break
                elif difflib.get_close_matches(
                    f"{first_l} {last_l}", hr_name_list, n=1, cutoff=0.88,
                ):
                    state = "hr_match"
                    fm = difflib.get_close_matches(
                        f"{first_l} {last_l}", hr_name_list, n=1, cutoff=0.88,
                    )
                    if fm:
                        hr_match_row = hr_info_by_name.get(fm[0])
                elif _matches_room_roster(roster_names, first, last):
                    state = "roster_match"
                elif u.get("_regex_non_person"):
                    # Pattern-matched as a non-person account AND no
                    # authoritative source claimed it. Flag as non_person
                    # rather than dropping — keeps it visible in the
                    # "non_person" directory filter for IT review.
                    state = "non_person"
                else:
                    state = "unmatched"

                # LOA detection (only meaningful for hr_match rows).
                hr_notes_val = (hr_match_row.notes or "") if hr_match_row else ""
                on_leave_flag = _is_on_leave_hr(hr_notes_val) if hr_notes_val else False

                # Email mismatch detection is disabled. The original
                # intent was "alert IT when HR adds a NEW name-change
                # note that we haven't acted on yet" — but detecting
                # "new since last sync" requires tracking previous
                # notes state. Retroactively flagging every existing
                # Née/Formerly entry just creates noise for accounts
                # that are already correct (the district's historical
                # maiden-name references are informational, not
                # action items). Leaving the flag column, endpoint,
                # and UI badge in place as dormant scaffolding; a
                # future pass can wire up real delta detection by
                # snapshotting hr_staff_cache.notes before rebuild.
                email_mismatch_flag = False

                by_state[state] += 1
                # Only genuinely unmatched ACTIVE accounts get auto-
                # queued for deprovision review. non_person (regex-
                # flagged) stays in the directory under the non_person
                # filter. Suspended accounts are already effectively
                # deprovisioned — queueing them would just create
                # noise for IT and spam the deprov list with people
                # on leave of absence.
                is_suspended = (u.get("status") or "").lower() == "suspended"
                if state == "unmatched" and not is_suspended:
                    unmatched_users.append({
                        "email": em, "first_name": first, "last_name": last,
                        "building": building,
                        "title": u.get("title") or "",
                    })

                db.add(StaffDirectoryEntry(
                    email=u["email"],
                    first_name=u["first_name"],
                    last_name=u["last_name"],
                    full_name=u.get("full_name"),
                    title=u.get("title"),
                    department=u.get("department"),
                    org_unit=u.get("org_unit"),
                    building=building,
                    phone=u.get("phone"),
                    status=u.get("status", "active"),
                    is_admin=u.get("is_admin", False),
                    last_login=u.get("last_login"),
                    google_id=u.get("google_id"),
                    match_state=state,
                    # Persist aliases so downstream HR-diff can recognize
                    # maiden/legacy addresses without calling Google again.
                    google_aliases=_json.dumps(aliases) if aliases else None,
                    hr_notes=hr_notes_val or None,
                    on_leave=on_leave_flag,
                    email_mismatch=email_mismatch_flag,
                    # 2FA / 2SV. .get() returns None when Google didn't
                    # include the field — kept as None (nullable) so we
                    # can tell "unknown" apart from "explicitly false".
                    is_enrolled_in_2sv=u.get("is_enrolled_in_2sv"),
                    is_enforced_in_2sv=u.get("is_enforced_in_2sv"),
                    cached_at=datetime.now(timezone.utc),
                ))

            await db.commit()
            result["synced"] = len(users)
            result["by_state"] = by_state

            # ── Reconcile auto-deprovision queue ──────────────────────
            # Two-way reconciliation on every sync:
            #   1. Queue new deprovisions for accounts that became
            #      unmatched (and don't already have an active entry).
            #   2. Auto-dismiss queue entries for accounts that are now
            #      confirmed (matched HR/roster/override) so improvements
            #      to matching logic heal stale queue rows without
            #      manual intervention.
            # Only auto_unmatched-sourced entries are auto-dismissed;
            # hr_sync provision entries and manual entries are left
            # alone regardless of current match_state.
            # Dedup set includes BOTH active rows AND recently-dismissed
            # rows. Without the dismissed-window guard, anyone an operator
            # manually dismissed gets re-queued on the next sync because
            # they're still unmatched in HR — the dismissal becomes
            # invisible noise. The 60-day window is long enough that HR
            # catches up on legitimate new-hire data entry, short enough
            # that a real later departure eventually re-flags.
            DISMISS_RESPAWN_WINDOW_DAYS = 60
            already_queued = (await db.execute(text("""
                SELECT LOWER(email) FROM staff_queue
                WHERE action = 'deprovision'
                  AND (
                    status IN ('pending_data', 'pending', 'ready', 'provisioning')
                    OR (
                      status = 'dismissed'
                      AND completed_at > now() - (:days || ' days')::interval
                    )
                  )
            """).bindparams(days=DISMISS_RESPAWN_WINDOW_DAYS))).all()
            already_queued_set = {r[0] for r in already_queued if r[0]}

            # 1) Queue new unmatched accounts
            unmatched_emails = {u["email"] for u in unmatched_users}
            queued_new = 0
            for u in unmatched_users:
                em = u["email"]
                if em in already_queued_set:
                    continue
                await db.execute(text("""
                    INSERT INTO staff_queue (
                        action, first_name, last_name, email, building,
                        title, source, status, expected_email, created_at
                    ) VALUES (
                        'deprovision', :first, :last, :email, :building,
                        :title, 'auto_unmatched', 'pending_data', :email, :ts
                    )
                """).bindparams(
                    first=u["first_name"], last=u["last_name"],
                    email=em, building=u["building"] or "",
                    title=u["title"], ts=datetime.now(timezone.utc),
                ))
                queued_new += 1

            # 2) Auto-dismiss queue entries whose emails are now
            #    confirmed by the new match logic. Scope: only
            #    source='auto_unmatched' so we don't touch manual
            #    entries or HR-sync provisions.
            #
            # Fetch existing auto_unmatched queue emails, take the set
            # difference in Python, then UPDATE by id. Avoids SQL
            # parameter-list landmines with empty/large sets.
            existing_rows = (await db.execute(text("""
                SELECT id, LOWER(email) AS em
                FROM staff_queue
                WHERE action = 'deprovision'
                  AND source = 'auto_unmatched'
                  AND status IN ('pending_data', 'pending', 'ready')
            """))).all()
            stale_ids = [row[0] for row in existing_rows if row[1] and row[1] not in unmatched_emails]
            auto_dismissed = 0
            if stale_ids:
                await db.execute(text("""
                    UPDATE staff_queue
                    SET status = 'dismissed',
                        completed_at = :ts,
                        error = COALESCE(error, '') || ' [auto-dismissed: now matched]'
                    WHERE id = ANY(:ids)
                """).bindparams(ts=datetime.now(timezone.utc), ids=stale_ids))
                auto_dismissed = len(stale_ids)

            if queued_new or auto_dismissed:
                await db.commit()
                if queued_new:
                    result["queued_deprovisions"] = queued_new
                if auto_dismissed:
                    result["auto_dismissed"] = auto_dismissed
                logger.info(
                    f"Staff sync: queued {queued_new} new auto_unmatched, "
                    f"auto-dismissed {auto_dismissed} now-matched"
                )

    except Exception as e:
        logger.error(f"sync_staff_directory failed: {e}")
        result["error"] = str(e)[:200]
        raise

    logger.info(f"Staff directory synced: {result['synced']} users — {result.get('by_state')}")

    # Trigger reconciliation with 30-second dedup window
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings
        settings = get_settings()
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        window = int(datetime.now(timezone.utc).timestamp() // 30)
        await redis.enqueue_job("run_staff_reconciliation", _job_id=f"reconciliation:{window}")
        logger.info("Enqueued staff reconciliation after staff sync")
    except Exception as e:
        logger.warning(f"Failed to enqueue reconciliation: {e}")

    return result
