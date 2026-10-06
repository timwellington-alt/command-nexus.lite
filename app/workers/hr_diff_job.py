"""
HR sheet diff job — compares HR Excel/Sheets data against staff directory.

Detects new hires (on HR, not in directory) and departures (in directory, not on HR).
Creates staff_queue entries for admin review.

Runs daily via scheduler.
"""

import difflib
import json
import logging
import re
from datetime import datetime, timezone

from app.workers.hr_sync_job import parse_former_last_name

logger = logging.getLogger(__name__)

# Fuzzy-match threshold for departure detection. Tight enough to catch
# typos ("Sarah"/"Sara") but loose enough that unrelated names don't
# collide. 0.88 is empirically the sweet spot for short "first last"
# strings.
_FUZZY_CUTOFF = 0.88


def canon_name_part(s: str) -> str:
    """
    Collapse a name part for punctuation-tolerant matching.

    Strips every non-alphanumeric character and lowercases the result.
    Used as a fallback after exact match fails so that "St. Onge",
    "St Onge", and "Stonge" all compare equal; "O'Brien" == "OBrien";
    "Byers-Johnson" == "byersjohnson"; "Van Buren" == "vanburen".
    """
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


async def check_hr_diffs(ctx: dict) -> dict:
    """Compare HR sheet against staff directory and queue diffs."""
    from app.db.engine import AsyncSessionLocal
    from app.modules.settings.repository import get_setting_value
    from sqlalchemy import text

    result = {
        "job": "check_hr_diffs",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "new_hires": 0,
        "departures": 0,
        "changes": 0,
        "error": None,
    }

    try:
        async with AsyncSessionLocal() as db:
            # Read HR sheet (SMB first, then Google Sheets fallback)
            hr_staff = None
            smb_server = await get_setting_value(db, "hr_smb", "server")
            if smb_server:
                try:
                    from app.integrations.smb.adapter import SmbExcelAdapter
                    hr_staff = await SmbExcelAdapter(db).read_hr_staff()
                except Exception as e:
                    logger.warning(f"SMB HR read failed: {e}")

            if hr_staff is None:
                sheet_id = await get_setting_value(db, "google", "hr_sheet_id")
                if sheet_id:
                    try:
                        from app.integrations.google.sheets_adapter import GoogleSheetsAdapter
                        hr_staff = await GoogleSheetsAdapter(db).read_hr_staff(sheet_id)
                    except Exception as e:
                        logger.warning(f"Google Sheets HR read failed: {e}")

            if not hr_staff:
                result["error"] = "No HR data source available"
                return result

            logger.info(f"HR diff: loaded {len(hr_staff)} records from HR sheet")

            # Building resolver — HR's SCHOOL column is free text
            # ("the district Elementary School"). Downstream provisioning
            # profiles + label-printer + notification lookups are keyed
            # by canonical codes (PES, EPE, PHS). Resolve once here so
            # every staff_queue row carries the code, not the display
            # name.
            from app.modules.settings.buildings import (
                get_building_maps,
                resolve_building_code_sync,
            )
            building_maps = await get_building_maps(db)

            # Load current staff directory.
            #
            # Two lookup structures — email-set + name-set — because HR's
            # legal name often produces an "expected" email (Andrea.Edwards)
            # that doesn't match the actual Google address (Andi.Edwards).
            # Without the name-set, every nickname-emailed teacher looked
            # like a "new hire" and got queued every run.
            dir_result = await db.execute(text(
                "SELECT email, first_name, last_name, building, title, google_aliases "
                "FROM staff_directory WHERE status = 'active'"
            ))
            directory = {}
            dir_emails: set[str] = set()
            dir_names: set[str] = set()
            from app.modules.staff.nicknames import get_nickname_variants as _nick_variants
            from app.workers.hr_sync_job import _last_name_variants as _ln_variants
            for row in dir_result.all():
                email = (row[0] or "").lower().strip()
                if email:
                    directory[email] = {
                        "first_name": row[1],
                        "last_name": row[2],
                        "building": row[3],
                        "title": row[4],
                    }
                    dir_emails.add(email)
                # Aliases — Google keeps rename-aliases here, which is the
                # only way to match maiden-name HR rows against the current
                # (post-rename) directory row.
                raw_aliases = row[5]
                if raw_aliases:
                    try:
                        for a in json.loads(raw_aliases):
                            if a:
                                dir_emails.add(str(a).lower())
                    except (json.JSONDecodeError, TypeError):
                        pass
                fn = (row[1] or "").strip().lower()
                ln = (row[2] or "").strip().lower()
                if fn and ln:
                    for lv in _ln_variants(ln):
                        for fv in [fn] + list(_nick_variants(fn)):
                            dir_names.add(f"{fv} {lv}")

            # ── Safety gate: refuse if staff_directory looks mid-refresh ──
            # See hr_sync_job._auto_queue_hr_diff for full context.
            #
            # Fires against two floors:
            #   1. Absolute row count — hard bottom, no HR sheet is
            #      smaller than 50 rows in practice.
            #   2. Ratio vs HR — Google directory is always LARGER than
            #      HR (retirees, subs, service accounts, board members).
            #      A healthy sheet-to-directory ratio is ~2x. If dir
            #      < 1.5x HR, something's wrong. 2026-07-28 incident:
            #      a transient Google 500 on /Users-Teachers dropped
            #      dir from 462 → 140, ratio 0.74 sailed past the old
            #      0.5 gate and 100 real teachers got auto-queued as
            #      "new hires." Bumping to 1.5x + expected floor catches
            #      the same shape.
            #   3. Historical baseline (new) — check the most recent
            #      *successful* run count. Abort if dir shrank by more
            #      than 25% since then, regardless of ratio to HR.
            _MIN_DIR_ABSOLUTE = 200
            _MIN_DIR_RATIO = 1.5
            _MAX_SHRINK = 0.25
            hr_count = len(hr_staff)
            dir_count = len(directory)
            if dir_count < _MIN_DIR_ABSOLUTE:
                msg = (f"staff_directory has {dir_count} rows (< {_MIN_DIR_ABSOLUTE} floor) "
                       f"— refusing to run diff; probable mid-refresh or partial-fetch outage")
                logger.error("check_hr_diffs gate: %s", msg)
                result["error"] = msg
                return result
            if hr_count and dir_count < hr_count * _MIN_DIR_RATIO:
                msg = (f"staff_directory has {dir_count} rows, HR has {hr_count} "
                       f"(< {_MIN_DIR_RATIO:.1f}x ratio) — refusing to run diff; "
                       f"probable mid-refresh")
                logger.error("check_hr_diffs gate: %s", msg)
                result["error"] = msg
                return result

            # Load school names for building display
            names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
            try:
                school_names = json.loads(names_raw)
            except Exception:
                school_names = {}

            # Build HR lookup by email and by name
            domain = await get_setting_value(db, "google", "domain") or ""
            email_template = await get_setting_value(db, "ad", "staff_email_template") or "{first}.{last}@{domain}"

            from app.modules.staff.service import build_staff_email

            from app.modules.staff.nicknames import get_nickname_variants

            hr_by_email = {}
            hr_by_name = {}
            # Name set used for departure lookup — includes every HR entry
            # with all of its nickname variants so "Mike Smith" in HR can
            # match "Michael Smith" in Google Workspace and vice versa.
            hr_name_set: set[str] = set()
            for h in hr_staff:
                first = (h.get("first_name") or "").strip()
                last = (h.get("last_name") or "").strip()
                if not first or not last:
                    continue

                hr_email = (h.get("email") or "").lower().strip()
                if not hr_email and domain:
                    hr_email = build_staff_email(first, last, domain, email_template)

                name_key = f"{first} {last}".lower().strip()
                entry = {
                    "first_name": first,
                    "last_name": last,
                    "email": hr_email,
                    "school": h.get("school", ""),
                    "classification": h.get("classification", ""),
                    "position": h.get("position", ""),
                }
                if hr_email:
                    hr_by_email[hr_email] = entry
                hr_by_name[name_key] = entry

                hr_name_set.add(name_key)
                first_l = first.lower()
                last_l = last.lower()
                for variant in get_nickname_variants(first_l):
                    hr_name_set.add(f"{variant} {last_l}")

            # Load existing pending queue items to avoid duplicates
            pending = await db.execute(text(
                "SELECT email, action FROM staff_queue WHERE status IN ('pending', 'pending_data', 'ready', 'confirmed', 'provisioning')"
            ))
            already_queued = {(r[0] or "").lower(): r[1] for r in pending.all()}

            # NOTE: deprovision detection used to live here. As of a031 it
            # moved into sync_staff_directory which uses positive-confirmation
            # matching (HR ∪ roster ∪ override) instead of the old
            # absence-of-presence diff. This job now ONLY queues new hires.

            # ── Detect new hires: on HR sheet but not in directory ──
            new_hires = 0
            # Helper: is this HR person already in the directory? Checks
            # email + name-set with nickname / hyphen / maiden variants
            # so we don't false-positive on Andi/Andrea, Becky/Rebecca,
            # or Née: Buffington → Carver-style renames.
            from app.workers.hr_sync_job import _strip_suffix as _hr_strip_suffix
            def _already_provisioned(hr_email: str, first: str, last: str, notes: str | None) -> bool:
                em = _hr_strip_suffix((hr_email or "").lower())
                if em and em in dir_emails:
                    return True
                # Local-part fallback — HR's built email may use a name
                # variant we don't have as a nickname (Ronald → Buck).
                # If the local part alone matches a directory address at
                # any domain, trust that.
                if em and "@" in em:
                    local = em.split("@", 1)[0]
                    for de in dir_emails:
                        if de.startswith(f"{local}@"):
                            return True
                fn = (first or "").strip().lower()
                ln = (last or "").strip().lower()
                if not fn or not ln:
                    return False
                first_variants = [fn] + list(_nick_variants(fn))
                last_variants = _ln_variants(ln)
                # Née: Buffington → also match jennifer.buffington@
                former = parse_former_last_name(notes)
                if former:
                    last_variants = list(
                        set(last_variants) | set(_ln_variants(former.lower()))
                    )
                for fv in first_variants:
                    for lv in last_variants:
                        if f"{fv} {lv}" in dir_names:
                            return True
                return False

            for email, hr in hr_by_email.items():
                if _already_provisioned(email, hr.get("first_name",""), hr.get("last_name",""), hr.get("notes")):
                    continue
                if email in already_queued:
                    continue
                # Derive building/role from HR classification
                raw_school = hr.get("school", "") or ""
                building = resolve_building_code_sync(raw_school, building_maps) or raw_school
                classification = (hr.get("classification") or "").lower()
                role_type = "teacher"
                if "admin" in classification or "principal" in classification:
                    role_type = "admin"
                elif "classified" in classification or "aide" in classification:
                    role_type = "classified"
                elif "tech" in classification:
                    role_type = "tech"
                elif "sub" in classification:
                    role_type = "sub"

                await db.execute(text("""
                    INSERT INTO staff_queue (action, first_name, last_name, email, building, role_type, title, details, source, status, position, classification, school, expected_email)
                    VALUES ('provision', :first, :last, :email, :building, :role, :title, :details, 'hr_sync', 'pending_data', :position, :classification, :school, :email)
                """).bindparams(
                    first=hr["first_name"], last=hr["last_name"], email=email,
                    building=building, role=role_type, title=hr.get("position", ""),
                    details=json.dumps({"classification": hr.get("classification", ""), "school": building}),
                    position=hr.get("position", ""), classification=hr.get("classification", ""),
                    school=building,
                ))
                new_hires += 1

            if new_hires:
                from app.audit.service import log_action
                await log_action(
                    db, actor="system", action="staff.hr_diff",
                    module="staff",
                    target=f"{new_hires} new hires queued",
                )

            await db.commit()
            result["new_hires"] = new_hires
            logger.info(f"HR diff: {new_hires} new hires queued")

    except Exception as e:
        logger.error(f"check_hr_diffs failed: {e}")
        result["error"] = str(e)[:200]
        raise

    return result


async def _load_room_roster_names(db) -> list[dict]:
    """
    Load every name currently on a building room roster.

    Each entry stores the original name plus two parsed interpretations:
        "last_is_last"  — last word is last name, everything before
                          is the first name ("Bobbi Jo Bricker" →
                          first="bobbi jo", last="bricker"). Handles
                          multi-word first names.
        "first_is_first" — first word is first name, everything after
                          is last name ("John Van Buren" → first="john",
                          last="van buren"). Handles multi-word last
                          names.

    The matcher checks both interpretations so either pattern matches.
    Entries with only one word are stored as last-name-only.
    Entries starting with an initial ("J. Smith") set first_initial.
    """
    from sqlalchemy import text

    try:
        rows = (await db.execute(text(
            "SELECT building, name FROM room_roster_cache "
            "WHERE name IS NOT NULL AND name != ''"
        ))).all()
    except Exception as e:
        logger.warning(f"Could not load room_roster_cache: {e}")
        return []

    entries = []
    for building, raw in rows:
        name = (raw or "").strip().rstrip("*?").strip()
        if not name:
            continue
        parts = name.split()
        bldg = (building or "").upper()
        full_lower = " ".join(p.lower() for p in parts)

        if len(parts) == 1:
            # Last name only
            entries.append({
                "building": bldg,
                "full": full_lower,
                "last": parts[0].lower(),
                "first": None,
                "first_initial": None,
                "alt_last": None,
                "alt_first": None,
            })
            continue

        first_token = parts[0].rstrip(".")
        is_initial = len(first_token) == 1 or parts[0].endswith(".")
        first_initial = first_token[0].lower() if first_token else None

        if is_initial:
            # "J. Smith" style — keep as initial-only entry
            entries.append({
                "building": bldg,
                "full": full_lower,
                "last": " ".join(p.lower() for p in parts[1:]),
                "first": None,
                "first_initial": first_initial,
                "alt_last": None,
                "alt_first": None,
            })
            continue

        # Primary interpretation: first word is first name, rest is last
        first_a = parts[0].lower()
        last_a = " ".join(p.lower() for p in parts[1:])

        # Alternate interpretation: last word is last name, rest is first
        first_b = " ".join(p.lower() for p in parts[:-1])
        last_b = parts[-1].lower()

        entries.append({
            "building": bldg,
            "full": full_lower,
            # Primary (first-is-first) — handles normal "John Smith"
            "first": first_a,
            "last": last_a,
            "first_initial": first_initial,
            # Alternate (last-is-last) — handles "Bobbi Jo Bricker"
            "alt_first": first_b if first_b != first_a else None,
            "alt_last": last_b if last_b != last_a else None,
        })
    return entries


def _matches_room_roster(roster_names: list[dict], first: str, last: str) -> bool:
    """
    Return True if the given Google staff name matches any room roster entry.

    Works against two parsed interpretations per roster entry (first-
    is-first AND last-is-last) so both "John Smith" and "Bobbi Jo
    Bricker" match correctly. First-word nickname expansion handles
    Mike/Michael, Bobbi/Bobbie, Gabby/Gabrielle, etc.
    """
    from app.modules.staff.nicknames import get_nickname_variants

    first_l = (first or "").strip().lower()
    last_l = (last or "").strip().lower()
    if not last_l:
        return False

    # Build Google candidate first-word variants. "Bobbie Jo" →
    # first_first_word "bobbie" → variants {"bobbi", "bobbie"}.
    first_first_word = first_l.split()[0] if first_l else ""
    first_variants = get_nickname_variants(first_first_word) | {first_first_word} if first_first_word else set()
    first_initial = first_first_word[0] if first_first_word else ""

    # Google "last word of last name" — handles hyphenated / multi-word
    # last names like "Van Buren" or "Sanders-Johnson" against roster
    # entries that only have the final token.
    last_last_word = last_l.split()[-1] if last_l else last_l

    # Canonicalized last name for punctuation-tolerant fallback match.
    # Matches "St Onge" / "St. Onge" / "Stonge" against each other,
    # and handles apostrophes and hyphens the same way.
    last_canon = canon_name_part(last_l)

    def _last_matches(entry_last: str | None) -> bool:
        """True if the entry's last name matches the Google last name
        via exact, last-word, or canonical comparison."""
        if not entry_last:
            return False
        if entry_last in (last_l, last_last_word):
            return True
        return canon_name_part(entry_last) == last_canon

    def _first_word_matches(entry_first: str | None) -> bool:
        if not entry_first:
            return False
        return entry_first.split()[0] in first_variants

    for entry in roster_names:
        # Primary interpretation: first-is-first
        if _last_matches(entry.get("last")):
            # Last-name-only hit
            if entry.get("first") is None and entry.get("first_initial") is None:
                return True
            # First-initial hit
            if entry.get("first") is None and entry.get("first_initial"):
                if entry["first_initial"] == first_initial:
                    return True
                continue
            # Full first-name hit
            if _first_word_matches(entry.get("first")):
                return True

        # Alternate interpretation: last-is-last. "Bobbi Jo Bricker"
        # stored as alt_first="bobbi jo", alt_last="bricker".
        if _last_matches(entry.get("alt_last")):
            if _first_word_matches(entry.get("alt_first")):
                return True

    return False
