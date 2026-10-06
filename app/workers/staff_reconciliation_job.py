"""
Staff reconciliation job — pre-computes cross-system matching.

Replaces per-request enrichment in the directory/profile endpoints.
Runs after each data sync and daily as a fallback.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.staff.nicknames import FORMAL_TO_NICKS, get_nickname_variants

logger = logging.getLogger(__name__)

# Assignments that should skip room-extension mismatch checks
_SKIP_ASSIGNMENTS = {"is", "md", "esc", "aide", "psych", "speech", "literacy coach", "counseling"}


async def run_staff_reconciliation(ctx: dict) -> dict:
    """
    Build the staff_reconciliation table from all source caches.

    Flow:
    1. Load all source tables into memory dicts
    2. For each StaffDirectoryEntry, match to every system
    3. DELETE + bulk INSERT into staff_reconciliation
    """
    from app.db.engine import AsyncSessionLocal
    from app.modules.staff.models import (
        StaffDirectoryEntry, StaffReconciliation,
        StaffIgnore, StaffLink,
        HRStaffCache, PaxtonUserCache, ADUserCache,
    )
    from app.modules.settings.repository import get_setting_value
    from app.modules.staff.room_sync import _parse_building_prefixes

    result = {
        "job": "run_staff_reconciliation",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reconciled": 0,
        "error": None,
    }

    t0 = time.monotonic()

    try:
        async with AsyncSessionLocal() as db:
            # ── Load all source data ────────────────────────────────

            # Staff directory (Google — source of truth)
            staff_result = await db.execute(select(StaffDirectoryEntry))
            all_staff = staff_result.scalars().all()

            # HR cache
            hr_emails: set[str] = set()
            hr_names: set[str] = set()
            hr_data: dict[str, dict] = {}
            try:
                hr_result = await db.execute(select(HRStaffCache))
                for h in hr_result.scalars().all():
                    entry = {
                        "email": h.email or "", "name": h.name or "",
                        "position": h.position, "school": h.school,
                        "classification": h.classification,
                    }
                    if h.email:
                        hr_emails.add(h.email.lower())
                        hr_data[h.email.lower()] = entry
                    name_key = (h.name or "").lower().strip()
                    if name_key:
                        hr_names.add(name_key)
                        if name_key not in hr_data:
                            hr_data[name_key] = entry
            except Exception as e:
                logger.warning(f"Reconciliation: HR cache read failed: {e}")

            # Paxton cache
            paxton_by_email: dict[str, dict] = {}
            paxton_by_name: dict[str, dict] = {}
            paxton_by_lf: dict[str, dict] = {}
            paxton_users: list[dict] = []
            try:
                pax_result = await db.execute(select(PaxtonUserCache))
                for u in pax_result.scalars().all():
                    entry = {
                        "id": u.paxton_id, "first_name": u.first_name,
                        "last_name": u.last_name, "display_name": u.display_name,
                        "email": u.email, "has_image": u.has_image,
                        "enabled": u.enabled,
                    }
                    paxton_users.append(entry)
                    em = (u.email or "").lower()
                    if em:
                        paxton_by_email[em] = entry
                    dn = " ".join((u.display_name or "").strip().lower().split())
                    if dn:
                        paxton_by_name[dn] = entry
                    fn = (u.first_name or "").strip().lower()
                    ln = (u.last_name or "").strip().lower()
                    if fn and ln:
                        paxton_by_lf[f"{fn} {ln}"] = entry
            except Exception as e:
                logger.warning(f"Reconciliation: Paxton cache read failed: {e}")

            # AD cache
            ad_by_email: dict[str, dict] = {}
            ad_by_name: dict[str, dict] = {}
            try:
                ad_result = await db.execute(select(ADUserCache))
                for a in ad_result.scalars().all():
                    entry = {
                        "username": a.username, "email": a.email,
                        "first_name": a.first_name, "last_name": a.last_name,
                        "display_name": a.display_name, "enabled": a.enabled,
                    }
                    ae = (a.email or "").lower()
                    if ae:
                        ad_by_email[ae] = entry
                    aname = f"{(a.first_name or '').strip()} {(a.last_name or '').strip()}".lower().strip()
                    if aname:
                        ad_by_name[aname] = entry
            except Exception as e:
                logger.warning(f"Reconciliation: AD cache read failed: {e}")

            # SIS/Clever (savepoint — table may not exist)
            sis_emails: set[str] = set()
            try:
                async with db.begin_nested():
                    sis_result = await db.execute(text("SELECT email FROM clever_staff"))
                    sis_emails = {r[0].lower() for r in sis_result.all() if r[0]}
            except Exception as e:
                logger.warning(f"Reconciliation: SIS read failed: {e}")

            # Last door access (savepoint — table may not exist)
            last_door_by_name: dict[str, dict] = {}
            try:
                async with db.begin_nested():
                    door_result = await db.execute(text("""
                        SELECT DISTINCT ON (de.person_name)
                            de.person_name, de.event_time, de.door_name, d.building
                        FROM door_events de
                        LEFT JOIN doors d ON de.door_name LIKE '%' || d.name || '%'
                        WHERE de.person_name IS NOT NULL AND de.person_name != ''
                        ORDER BY de.person_name, de.event_time DESC
                    """))
                    for person_name, event_time, door_name, building in door_result.all():
                        clean_door = (door_name or "")
                        if " - " in clean_door:
                            clean_door = clean_door.split(" - ", 1)[1]
                        clean_door = clean_door.replace(" (In)", "").replace(" (Out)", "").strip()
                        last_door_by_name[person_name.lower().strip()] = {
                            "time": event_time.isoformat() if event_time else None,
                            "door": clean_door,
                            "building": building,
                        }
            except Exception as e:
                logger.warning(f"Reconciliation: Door events read failed: {e}")

            # Phone extensions from DB cache (savepoint — table may not exist)
            phone_by_email: dict[str, dict] = {}
            phone_by_name: dict[str, dict] = {}
            phone_by_last: dict[str, list[dict]] = {}
            try:
                async with db.begin_nested():
                    phone_result = await db.execute(text(
                        "SELECT extension, caller_id_name, email, department, "
                        "COALESCE(out_of_service, 'no'), ip, model "
                        "FROM phone_extension_cache"
                    ))
                    for ext, name, email_p, dept, out_of_service, ip, model in phone_result.all():
                        registered = (out_of_service or "no").lower() != "yes"
                        p = {
                            "extension": ext, "name": name or "", "email": email_p or "",
                            "department": dept or "", "registered": registered,
                            "ip": ip or "", "model": model or "",
                        }
                        if p["email"]:
                            phone_by_email[p["email"].lower()] = p
                        if p["name"]:
                            pname = p["name"].lower().strip()
                            phone_by_name[pname] = p
                            parts = pname.split()
                            if len(parts) >= 2:
                                phone_by_last.setdefault(parts[-1], []).append(p)
            except Exception as e:
                logger.warning(f"Reconciliation: Phone cache read failed: {e}")

            # Extension name map + building prefix map (for room-ext mismatch)
            ext_name_map: dict[str, str] = {}
            ext_prefix_map: dict[str, str] = {}
            try:
                async with db.begin_nested():
                    _ext_rows = await db.execute(text(
                        "SELECT extension, caller_id_name FROM phone_extension_cache"
                    ))
                    ext_name_map = {r[0]: r[1] for r in _ext_rows.all() if r[0] and r[1]}
                _bmap = await get_setting_value(db, "grandstream", "building_map") or ""
                ext_prefix_map = _parse_building_prefixes(_bmap)
            except Exception as e:
                logger.warning(f"Reconciliation: Extension name lookup failed: {e}")

            # Room roster (savepoint — table may not exist)
            # Store both by full name AND by (nickname-aware-first, canonical-last)
            # so lookups downstream can hit whichever shape the roster sheet uses.
            room_by_name: dict[str, dict] = {}
            # (canon_last) -> list of (first_word_lower, entry). Used for the
            # nickname / punctuation-tolerant fallback path below.
            room_by_canon_last: dict[str, list[tuple[str, dict]]] = {}
            try:
                from app.modules.staff.nicknames import get_nickname_variants
                from app.workers.hr_diff_job import canon_name_part
                async with db.begin_nested():
                    _room_rows = await db.execute(text(
                        "SELECT building, room, name, assignment, floor, COALESCE(is_esc, false) "
                        "FROM room_roster_cache"
                    ))
                    for r in _room_rows.all():
                        name_lower = r[2].lower().strip()
                        entry = {
                            "building": r[0], "room": r[1], "name": r[2],
                            "assignment": r[3], "floor": r[4], "is_esc": r[5],
                        }
                        room_by_name[name_lower] = entry
                        # Populate the canonical-last index. Try both
                        # "First Last" and "Last First" interpretations
                        # so "Madelyn Zickafoose" and "Zickafoose Madelyn"
                        # both surface via last-name lookup.
                        parts = [p for p in name_lower.split() if p]
                        if len(parts) >= 2:
                            # (first_word_first, all-but-first as last) — "Kenny Newman"
                            interps = [(parts[0], " ".join(parts[1:]))]
                            # (all-but-last as first, last-word as last) — "Bobbi Jo Bricker"
                            #  → first="bobbi jo" last="bricker"
                            multiw_first = (" ".join(parts[:-1]), parts[-1])
                            if multiw_first not in interps:
                                interps.append(multiw_first)
                            # (all-but-first as first, first-word as last) — "Bock Sarah"
                            #  → first="sarah" last="bock"
                            swap = (" ".join(parts[1:]), parts[0])
                            if swap not in interps:
                                interps.append(swap)
                            for first_l, last_l in interps:
                                canon = canon_name_part(last_l)
                                if canon:
                                    room_by_canon_last.setdefault(canon, []).append(
                                        (first_l.split()[0] if first_l else "", entry)
                                    )
            except Exception as e:
                logger.warning(f"Reconciliation: Room roster read failed: {e}")

            # Confirmed links
            link_result = await db.execute(
                select(StaffLink).where(StaffLink.confirmed == True)  # noqa: E712
            )
            confirmed_links: dict[str, int] = {}
            for lnk in link_result.scalars().all():
                if lnk.google_email:
                    confirmed_links[lnk.google_email.lower()] = lnk.paxton_id

            # Ignored usernames — only the legacy "hide from directory"
            # kind of ignore, not the new override model. kind='non_person'
            # rows are dropped upstream in sync_staff_directory so they
            # never reach this job. kind='confirmed' rows are real staff
            # and must NOT be flagged ignored — the reconciliation view
            # surfaces them with the Override pill instead.
            #
            # As of the override rewrite, no code path creates 'ignore'
            # rows that would reach this query, so the set is typically
            # empty and the .ignored column stays False district-wide.
            # The query stays in case any district has custom entries.
            ignored_result = await db.execute(
                select(StaffIgnore.username).where(
                    StaffIgnore.restored_at == None,  # noqa: E711
                    StaffIgnore.kind == "legacy_hide",  # never created today
                )
            )
            ignored_usernames = {r[0].lower() for r in ignored_result.all() if r[0]}

            # ── Reconcile each staff member ─────────────────────────
            now = datetime.now(timezone.utc)
            rows: list[StaffReconciliation] = []

            for s in all_staff:
                email_lower = (s.email or "").lower()
                username = email_lower.split("@")[0] if email_lower else ""
                name_key = f"{s.first_name} {s.last_name}".lower().strip()
                display_name = s.full_name or f"{s.first_name} {s.last_name}".strip()

                # ── HR matching ──
                hr_active = None
                hr_match = None
                if hr_emails or hr_names:
                    hr_active = (email_lower in hr_emails) or (name_key in hr_names)
                    hr_match = hr_data.get(email_lower) or hr_data.get(name_key)

                # ── Role type derivation ──
                role_type = None
                hr_position = None
                hr_school = None
                hr_classification = None
                if hr_match:
                    hr_position = hr_match.get("position")
                    hr_school = hr_match.get("school")
                    hr_classification = hr_match.get("classification")
                    cls = (hr_classification or "").lower().strip()
                    pos = (hr_position or "").lower().strip()
                    # District uses abbreviated HR classification codes
                    # (Cert = Certificated / teacher, Class = Classified,
                    # Adm = Administrative, Adm - Class = admin-track
                    # classified specialist, xmpt = exempt salaried
                    # admin). Match those AND the fuller strings that
                    # some HR rows use, then fall back to position
                    # keywords for edge cases like blank classification
                    # with "Assistant Principal" in position.
                    if cls in ("cert",) or "teacher" in cls or "instructor" in cls \
                            or cls.startswith("licens"):
                        role_type = "teacher"
                    elif cls in ("adm", "adm - class", "xmpt", "exempt") \
                            or "admin" in cls or "principal" in cls \
                            or "director" in cls or "superintendent" in cls:
                        role_type = "admin"
                    elif "sub" in cls or cls.startswith("substitut"):
                        role_type = "sub"
                    elif "tech" in cls:
                        role_type = "tech"
                    elif cls == "class" or "classified" in cls \
                            or "aide" in cls or "custod" in cls \
                            or "secret" in cls or "cook" in cls \
                            or "driver" in cls or "kitchen" in cls \
                            or "maint" in cls:
                        role_type = "classified"
                    elif not cls and pos:
                        # No classification — infer from position text
                        if "principal" in pos or "superintendent" in pos \
                                or "director" in pos or "coordinator" in pos:
                            role_type = "admin"
                        elif "teacher" in pos or "instructor" in pos \
                                or "counselor" in pos or "nurse" in pos \
                                or "librarian" in pos:
                            role_type = "teacher"
                        elif "aide" in pos or "custod" in pos or "secret" in pos \
                                or "cook" in pos or "driver" in pos \
                                or "maint" in pos:
                            role_type = "classified"
                    else:
                        role_type = cls

                if not role_type:
                    ou = (s.org_unit or "").lower()
                    if "teacher" in ou:
                        role_type = "teacher"
                    elif "admin-staff" in ou:
                        role_type = "admin"
                    elif "classified" in ou:
                        role_type = "classified"
                    elif "tech" in ou:
                        role_type = "tech"
                    elif "sub" in ou:
                        role_type = "sub"
                    elif "board" in ou:
                        role_type = "admin"

                # ── Paxton matching ──
                pax_match = None
                paxton_match_method = None
                if email_lower in confirmed_links:
                    pid = confirmed_links[email_lower]
                    pax_match = next((u for u in paxton_users if u["id"] == pid), None)
                    if pax_match:
                        paxton_match_method = "confirmed_link"
                if not pax_match and email_lower in paxton_by_email:
                    pax_match = paxton_by_email[email_lower]
                    paxton_match_method = "email"
                if not pax_match:
                    clean = " ".join(name_key.split())
                    pax_match = paxton_by_name.get(clean)
                    if pax_match:
                        paxton_match_method = "display_name"
                if not pax_match:
                    fn_l = (s.first_name or "").strip().lower()
                    ln_l = (s.last_name or "").strip().lower()
                    pax_match = paxton_by_lf.get(f"{fn_l} {ln_l}")
                    if pax_match:
                        paxton_match_method = "first_last"
                if not pax_match:
                    fn_l = (s.first_name or "").strip().lower().split()[0] if s.first_name else ""
                    ln_l = (s.last_name or "").strip().lower()
                    nick_variants = FORMAL_TO_NICKS.get(fn_l, set()) | {fn_l}
                    for nick in nick_variants:
                        pax_match = paxton_by_lf.get(f"{nick} {ln_l}")
                        if pax_match:
                            paxton_match_method = "nickname"
                            break

                # ── Fuzzy fallback ──
                # After exact + nickname miss, try a close-match against
                # the full first+last set. Catches spelling drift like
                # "Missi Wolfenbarker" (Google) ↔ "Missy Wolfenbarker"
                # (Paxton) — ratio ~0.96, easily above the 0.88 cutoff
                # the HR path uses. Also catches typos like a swapped
                # letter in the last name.
                if not pax_match and fn_l and ln_l:
                    import difflib
                    candidate = f"{fn_l} {ln_l}"
                    hit = difflib.get_close_matches(
                        candidate, list(paxton_by_lf.keys()), n=1, cutoff=0.88,
                    )
                    if hit:
                        pax_match = paxton_by_lf[hit[0]]
                        paxton_match_method = "fuzzy"

                # ── AD matching ──
                # District username convention (see feedback_district_username_convention):
                #   Staff:    firstname.lastname   (always contains a dot)
                #   Students: lastname{initial}{grad_year_2}   e.g. adkinsj31
                # If an AD-by-name fallback returns a student-pattern
                # username, it's ALMOST CERTAINLY the staff member's kid
                # (Jessica Adkins → daughter's adkinsj31 account); reject
                # it so the reconciled row keeps the email-prefix username
                # and stays disambiguated from the student profile.
                _student_pattern = re.compile(r"^[a-z]+[a-z]\d{2}$")
                _email_match = ad_by_email.get(email_lower)
                _name_match = ad_by_name.get(name_key)
                if _name_match and _student_pattern.match(
                    ((_name_match.get("username") or "").lower())
                ):
                    _name_match = None  # student-shaped username — skip
                ad_match = _email_match or _name_match
                ad_match_method = None
                ad_username_val = username  # default from email prefix
                if ad_match:
                    if ad_by_email.get(email_lower) == ad_match:
                        ad_match_method = "email"
                    else:
                        ad_match_method = "name"
                    ad_username_val = ad_match.get("username") or ad_username_val

                # ── Phone matching ──
                ucm_match = phone_by_email.get(email_lower)
                phone_match_method = None
                if ucm_match:
                    phone_match_method = "email"
                if not ucm_match:
                    display_name_lower = display_name.lower().strip()
                    ucm_match = phone_by_name.get(display_name_lower)
                    if ucm_match:
                        phone_match_method = "display_name"
                if not ucm_match:
                    fl_name = f"{(s.first_name or '').strip()} {(s.last_name or '').strip()}".lower().strip()
                    ucm_match = phone_by_name.get(fl_name)
                    if ucm_match:
                        phone_match_method = "first_last"
                if not ucm_match:
                    staff_first = (s.first_name or "").strip().lower().split()[0] if s.first_name else ""
                    staff_last = (s.last_name or "").strip().lower()
                    candidates = phone_by_last.get(staff_last, [])
                    if candidates and staff_first:
                        nick_variants = FORMAL_TO_NICKS.get(staff_first, set()) | {staff_first}
                        for c in candidates:
                            ucm_first = (c["name"] or "").lower().strip().split()[0]
                            if ucm_first in nick_variants or ucm_first == staff_first:
                                ucm_match = c
                                phone_match_method = "nickname"
                                break

                # ── Room roster matching ──
                room_match = None
                room_match_method = None
                ln_lower = (s.last_name or "").strip().lower()
                fn_lower = (s.first_name or "").strip().lower()
                fn_first_word = fn_lower.split()[0] if fn_lower else ""
                # Tier 1 — bare last name (roster has just "Zickafoose")
                room_match = room_by_name.get(ln_lower)
                if room_match:
                    room_match_method = "last_name"
                # Tier 2 — "F. Last" (roster has "M. Zickafoose")
                if not room_match:
                    fn_initial = (s.first_name or "").strip()[:1].upper() + "." if s.first_name else ""
                    room_match = room_by_name.get(f"{fn_initial} {s.last_name}".lower().strip())
                    if room_match:
                        room_match_method = "initial_last"
                # Tier 3 — full "First Last" or "Last First" match
                if not room_match:
                    for cand in (f"{fn_lower} {ln_lower}".strip(),
                                 f"{ln_lower} {fn_lower}".strip()):
                        room_match = room_by_name.get(cand)
                        if room_match:
                            room_match_method = "full_name"
                            break
                # Tier 4 — canonical last name + nickname first-word
                # (catches Becky↔Rebecca, Kenny↔Kenneth, Libby "St. Onge"
                # ↔ "St Onge").
                if not room_match and ln_lower and fn_first_word:
                    try:
                        from app.modules.staff.nicknames import get_nickname_variants
                        from app.workers.hr_diff_job import canon_name_part
                        canon = canon_name_part(ln_lower)
                        variants = get_nickname_variants(fn_first_word) | {fn_first_word}
                        for roster_first, entry in room_by_canon_last.get(canon, []):
                            if roster_first in variants:
                                room_match = entry
                                room_match_method = "nickname_canon"
                                break
                            # Reverse-check: roster has "Becky", staff is "Rebecca"
                            roster_variants = get_nickname_variants(roster_first) | {roster_first}
                            if fn_first_word in roster_variants:
                                room_match = entry
                                room_match_method = "nickname_canon"
                                break
                    except Exception as e:
                        logger.warning(f"Reconciliation: nickname-canon room match failed: {e}")

                # Tier 5 — single-letter spelling variance on first name,
                # scoped to same canonical last name. Catches nickname
                # table gaps like Madelyn↔Madalyn. Cutoff is lower
                # (0.80) than hr_diff_job's 0.88 because the candidate
                # set is already narrowed to same-last-name matches, so
                # false positive risk is much lower. Two teachers with
                # the same last name AND similar first names in the same
                # building would collide, but that's a real-world naming
                # collision the operator would catch on review.
                if not room_match and ln_lower and fn_first_word:
                    try:
                        import difflib
                        from app.workers.hr_diff_job import canon_name_part
                        canon = canon_name_part(ln_lower)
                        cands = room_by_canon_last.get(canon, [])
                        first_names = [rf for rf, _ in cands if rf]
                        m = difflib.get_close_matches(
                            fn_first_word, first_names, n=1, cutoff=0.80,
                        )
                        if m:
                            for roster_first, entry in cands:
                                if roster_first == m[0]:
                                    room_match = entry
                                    room_match_method = "fuzzy_first"
                                    break
                    except Exception as e:
                        logger.warning(f"Reconciliation: fuzzy room match failed: {e}")

                # ── Last door access ──
                display_lower = display_name.lower().strip()
                last_door = last_door_by_name.get(display_lower) or last_door_by_name.get(name_key)

                # ── SIS/Clever ──
                sis_ok = email_lower in sis_emails if sis_emails else None

                # ── Name sync issues ──
                google_name = f"{(s.first_name or '').strip()} {(s.last_name or '').strip()}".strip()
                name_issues = []
                if ucm_match:
                    ucm_name = (ucm_match.get("name") or "").strip()
                    if ucm_name and google_name and ucm_name.lower() != google_name.lower():
                        name_issues.append({
                            "system": "ucm", "current": ucm_name,
                            "extension": ucm_match["extension"],
                        })
                if pax_match:
                    pax_name = f"{(pax_match.get('first_name') or '').strip()} {(pax_match.get('last_name') or '').strip()}".strip()
                    if pax_name and google_name and pax_name.lower() != google_name.lower():
                        name_issues.append({
                            "system": "paxton", "current": pax_name,
                            "paxton_id": pax_match.get("id"),
                        })
                if ad_match:
                    ad_name = (ad_match.get("display_name") or "").strip()
                    if ad_name and google_name and ad_name.lower() != google_name.lower():
                        name_issues.append({
                            "system": "ad", "current": ad_name,
                            "username": ad_match.get("username"),
                        })

                # ── Room-extension mismatch ──
                room_ext_mismatch = None
                if room_match:
                    _assignment_lower = (room_match.get("assignment") or "").lower()
                    # Skip if either the ROSTER assignment OR the person's
                    # TITLE marks them as support staff. Aides in
                    # particular land on the roster with the classroom
                    # they help in (e.g., "2nd Grade"), so
                    # _assignment_lower alone lets them through — the
                    # title check catches them explicitly (2026-08-11 per
                    # Tim: "Aides don't need phone extensions assigned").
                    # Google's staff_directory.title is often blank for
                    # non-teaching staff; hr_position is the authoritative
                    # source in that case (e.g. Rachael Irwin whose
                    # Google title is NULL but HR position = "Aide -
                    # Educational"). Check both.
                    _effective_title = (
                        (s.title or "").strip()
                        or (hr_position or "").strip()
                    ).lower()
                    _is_support = (
                        room_match.get("is_esc")
                        or any(skip in _assignment_lower for skip in _SKIP_ASSIGNMENTS)
                        or any(skip in _effective_title for skip in _SKIP_ASSIGNMENTS)
                    )
                    if (room_match.get("room")
                            and not _is_support
                            and room_match.get("building", "").upper() == (s.building or "").upper()):
                        room_ext = ext_prefix_map.get(
                            room_match.get("building", "").upper(), ""
                        ) + room_match["room"]
                        room_ext_name = ext_name_map.get(room_ext)
                        if room_ext_name is not None:
                            staff_full = f"{(s.first_name or '').strip()} {(s.last_name or '').strip()}".strip().lower()
                            if (staff_full
                                    and staff_full not in room_ext_name.lower()
                                    and (s.last_name or "").strip().lower() not in room_ext_name.lower()):
                                room_ext_mismatch = {
                                    "extension": room_ext,
                                    "room": room_match["room"],
                                    "current_name": room_ext_name,
                                    "expected_name": f"{(s.first_name or '').strip()} {(s.last_name or '').strip()}".strip(),
                                }

                # ── Ignored status ──
                ignored = (
                    username.lower() in ignored_usernames
                    or email_lower.split("@")[0] in ignored_usernames
                )

                # ── Build row ──
                row = StaffReconciliation(
                    email=s.email,
                    username=ad_username_val,
                    first_name=s.first_name,
                    last_name=s.last_name,
                    full_name=s.full_name,
                    display_name=display_name,
                    title=s.title if s.title else (hr_position if hr_match and not s.title else s.title),
                    department=s.department,
                    building=s.building,
                    org_unit=s.org_unit,
                    phone=s.phone,
                    google_status=s.status,
                    is_admin=s.is_admin,
                    last_login=s.last_login,
                    google_id=s.google_id,
                    # Matched IDs
                    paxton_id=pax_match["id"] if pax_match else None,
                    ad_username=ad_match.get("username") if ad_match else None,
                    extension=ucm_match["extension"] if ucm_match else None,
                    room=room_match["room"] if room_match else None,
                    room_assignment=room_match["assignment"] if room_match else None,
                    room_floor=room_match["floor"] if room_match else None,
                    room_building=room_match["building"] if room_match else None,
                    is_esc=room_match.get("is_esc", False) if room_match else False,
                    hr_email=hr_match.get("email") if hr_match else None,
                    # Status flags
                    google_ok=s.status == "active",
                    ad_ok=ad_match.get("enabled", False) if ad_match else False,
                    sis_ok=sis_ok,
                    hr_active=hr_active,
                    paxton_ok=bool(pax_match),
                    has_paxton_photo=pax_match.get("has_image", False) if pax_match else False,
                    # Role
                    role_type=role_type,
                    hr_position=hr_position,
                    hr_school=hr_school,
                    hr_classification=hr_classification,
                    hr_notes=s.hr_notes,
                    on_leave=s.on_leave,
                    email_mismatch=s.email_mismatch,
                    # 2FA / 2SV — mirror from staff_directory. Nullable
                    # tri-state (True/False/None) preserved so the UI
                    # can show '—' for "unknown" separately from '✗'
                    # for "definitely not enrolled".
                    is_enrolled_in_2sv=s.is_enrolled_in_2sv,
                    is_enforced_in_2sv=s.is_enforced_in_2sv,
                    # Match confidence
                    paxton_match_method=paxton_match_method,
                    ad_match_method=ad_match_method,
                    phone_match_method=phone_match_method,
                    room_match_method=room_match_method,
                    # Names from each system
                    paxton_name=(
                        f"{(pax_match.get('first_name') or '').strip()} {(pax_match.get('last_name') or '').strip()}".strip()
                        if pax_match else None
                    ),
                    ad_display_name=ad_match.get("display_name") if ad_match else None,
                    phone_caller_id=ucm_match.get("name") if ucm_match else None,
                    # Computed issues (JSON)
                    name_sync_issues=json.dumps(name_issues) if name_issues else None,
                    room_ext_mismatch=json.dumps(room_ext_mismatch) if room_ext_mismatch else None,
                    # Door access
                    last_door_time=last_door.get("time") if last_door else None,
                    last_door_name=last_door.get("door") if last_door else None,
                    last_door_building=last_door.get("building") if last_door else None,
                    # Phone (static)
                    phone_registered=ucm_match.get("registered") if ucm_match else None,
                    phone_ip=ucm_match.get("ip", "") if ucm_match else None,
                    phone_model=ucm_match.get("model", "") if ucm_match else None,
                    # Match confirmation state — copied straight from the
                    # source so the directory UI can filter without joining.
                    match_state=getattr(s, "match_state", None),
                    # Meta
                    ignored=ignored,
                    reconciled_at=now,
                )
                rows.append(row)

            # ── Atomic replace ──
            await db.execute(delete(StaffReconciliation))
            db.add_all(rows)
            await db.commit()
            result["reconciled"] = len(rows)

    except Exception as e:
        logger.error(f"run_staff_reconciliation failed: {e}", exc_info=True)
        result["error"] = str(e)[:200]
        raise

    elapsed = time.monotonic() - t0
    logger.info(f"Staff reconciliation: {result['reconciled']} staff in {elapsed:.1f}s")
    return result
