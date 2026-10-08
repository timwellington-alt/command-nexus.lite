"""
HR staff data cache sync — pulls HR sheet data into local cache.

Runs 2x daily. Staff directory reads from cache instead of live SMB/Sheets.
After sync, diffs HR cache against staff_directory to auto-queue
provision/deprovision entries.
"""

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select, delete, text

logger = logging.getLogger(__name__)


# Suffix tokens HR staff sometimes append to a last-name column.
# HR's template has an email formula like
#   =LOWER(prefname & "." & lastname) & "@" & domain
# which pulls suffixes into both the last-name AND the computed email
# ("j.smith, jr.@example.edu"). We strip these
# everywhere we compare names or emails to the Google directory so
# "Robert Manchester, Jr." matches "Bob Manchester".
_SUFFIX_RE = re.compile(
    r"""
    [\s,]*                          # optional leading space/comma
    \b
    (?:
        jr\.?                       # Jr, Jr.
      | sr\.?                       # Sr, Sr.
      | ii|iii|iv|v                 # roman numerals
      | 2nd|3rd|4th                 # ordinals
      | ph\.?d\.?                   # PhD
      | m\.?d\.?                    # MD
    )
    \b\.?
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _strip_suffix(s: str) -> str:
    """Remove common name suffixes from a name or email local-part."""
    if not s:
        return s
    cleaned = _SUFFIX_RE.sub("", s).strip()
    # Collapse any double spaces / trailing punctuation left behind
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.")
    return cleaned


# HR's Notes column uses free text for leave status. Any of these
# phrases (case-insensitive, word boundary) flag the person as
# currently away. Sick Leave and Maternity are included for
# completeness even if the current district doesn't use them yet.
_LEAVE_PATTERNS = re.compile(
    r"\b(?:loa|lwop|fmla|"
    r"admin(?:istrative)?\s*leave|"
    r"medical\s*leave|"
    r"sick\s*leave|"
    r"maternity\s*leave|"
    r"paternity\s*leave|"
    r"personal\s*leave|"
    r"on\s+leave)\b",
    re.IGNORECASE,
)


def is_on_leave(notes: str | None) -> bool:
    """
    Return True if HR's free-text Notes column indicates the staff
    member is currently on leave. Matches LOA, FMLA, Admin Leave,
    Medical/Sick/Maternity/Paternity/Personal Leave, and generic
    "On Leave". Case-insensitive. Empty/None → False.
    """
    if not notes:
        return False
    return bool(_LEAVE_PATTERNS.search(notes))


# Name-change signals in HR Notes. "Née" is the French convention
# for maiden name; "Formerly" is the plain-English alternate. Both
# point at the OLD last name — HR's first_name/last_name columns
# already carry the CURRENT legal name.
#
# Observed variants in the live data:
#   "Née:  Shultz"
#   "Née: Buffington"
#   "Formerly: Brewer"
#   "New Née:  Malone"            (combined with new-hire flag)
_NAME_CHANGE_RE = re.compile(
    r"(?:n\u00e9e|nee|formerly)[\s:]+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?)",
    re.IGNORECASE,
)


def parse_former_last_name(notes: str | None) -> str | None:
    """
    Extract a prior last name from HR's Notes column.

    Returns the maiden or former surname in title case, or None if
    the notes don't contain a recognizable name-change marker. The
    return value is the OLD last name (the one Google may still be
    using); the CURRENT last name lives on the HR row itself.
    """
    if not notes:
        return None
    m = _NAME_CHANGE_RE.search(notes)
    if not m:
        return None
    return m.group(1).strip().title() or None


def _last_name_variants(last: str) -> list[str]:
    """
    Generate last-name variants for cross-side matching.

    People often appear in Google under ONE half of a hyphenated name
    (e.g. "Shay White") while HR lists the full compound form
    ("Shay Pennington-White"). Splitting in both directions lets
    either layout find a match.
    """
    if not last:
        return []
    last_lower = last.lower().strip()
    variants = {last_lower}
    # Hyphen split — each half + the full compound with space instead
    if "-" in last_lower:
        for part in last_lower.split("-"):
            p = part.strip()
            if p:
                variants.add(p)
        variants.add(last_lower.replace("-", " "))
        variants.add(last_lower.replace("-", ""))
    # Space-separated compound ("Van Dyke", "De La Cruz") → last token
    elif " " in last_lower:
        parts = last_lower.split()
        if parts:
            variants.add(parts[-1])
            variants.add("".join(parts))
    return list(variants)


async def sync_hr_data(ctx: dict) -> dict:
    """Sync HR staff data into hr_staff_cache."""
    from app.db.engine import AsyncSessionLocal
    from app.modules.staff.models import HRStaffCache

    result = {
        "job": "sync_hr_data",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "added": 0, "updated": 0, "removed": 0, "unchanged": 0, "error": None,
    }

    try:
        async with AsyncSessionLocal() as db:
            from app.modules.settings.repository import get_setting_value
            import json as _json

            # Lite data source — per-building Google Sheet config at
            # hr_sheets.buildings. JSON blob written by the Settings UI
            # builder: {"PHS": {"sheet_id": "...", "range": "..."}, ...}
            # The building code wins — whatever the sheet says in its
            # "Building" column is ignored at ingest.
            hr_records: list[dict] = []
            raw = await get_setting_value(db, "hr_sheets", "buildings")
            try:
                sheets_cfg = _json.loads(raw or "{}")
            except Exception:
                sheets_cfg = {}

            if sheets_cfg:
                try:
                    from app.integrations.google.sheets_adapter import GoogleSheetsAdapter
                    adapter = GoogleSheetsAdapter(db)
                    for bcode, cfg in sheets_cfg.items():
                        sheet_id = (cfg or {}).get("sheet_id", "").strip()
                        if not sheet_id:
                            continue
                        tab_range = ((cfg or {}).get("range") or "Staff Directory!A:K").strip()
                        try:
                            rows = await adapter.read_sheet(sheet_id, tab_range)
                        except Exception as e:
                            logger.warning(f"HR sheet read failed for {bcode}: {e}")
                            continue
                        # First non-empty row = headers. Match by lower-
                        # cased header text so operator renames of the
                        # friendly label (e.g. "Email" vs "email") still
                        # resolve.
                        if not rows:
                            continue
                        headers = [(h or "").strip().lower().replace(" ", "_")
                                   for h in rows[0]]
                        for data_row in rows[1:]:
                            if not any((c or "").strip() for c in data_row):
                                continue
                            rec = {}
                            for i, col in enumerate(data_row):
                                if i >= len(headers):
                                    break
                                key = headers[i]
                                # Normalize common header variants to the
                                # keys hr_sync downstream expects.
                                key = {
                                    "position_/_title": "position",
                                    "preferred_name": "preferred_name",
                                    "phone_ext.": "extension",
                                    "oh_cert_#": "cert_number",
                                    "hr_notes": "notes",
                                }.get(key, key)
                                rec[key] = col
                            # Config building code wins
                            rec["school"] = bcode
                            rec["source_tab"] = bcode
                            hr_records.append(rec)
                except Exception as e:
                    logger.warning(f"Per-building HR sheets ingest failed: {e}")

            if not hr_records:
                result["error"] = (
                    "No HR data — configure at least one building sheet in "
                    "Settings → Staff → HR Google Sheets."
                )
                return result

            # Full replace — clear and rebuild
            # This avoids key mismatches when switching between SMB and Sheets sources
            await db.execute(delete(HRStaffCache))

            # Load building resolver maps once so we can canonicalize
            # HR's free-text school values in a tight loop.
            from app.modules.settings.buildings import get_building_maps, resolve_building_code_sync
            building_maps = await get_building_maps(db)

            now = datetime.now(timezone.utc)
            seen_keys = set()
            unresolved_buildings: dict[str, int] = {}
            for r in hr_records:
                email = (r.get("email") or "").strip().lower()
                first = (r.get("first_name") or "").strip()
                last = (r.get("last_name") or "").strip()
                name = f"{first} {last}".strip()
                tab = r.get("source_tab") or r.get("tab_source") or ""

                # Dedup by name (primary) or email (fallback)
                dedup_key = name.lower() if name else email
                if not dedup_key or dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                # Translate HR's free-text school to the internal code
                # at the ingestion boundary, so downstream queries never
                # see "District - BLDG" or "District High School".
                raw_school = r.get("school", "") or ""
                resolved = resolve_building_code_sync(raw_school, building_maps)
                if resolved is None and raw_school.strip():
                    unresolved_buildings[raw_school] = unresolved_buildings.get(raw_school, 0) + 1
                canonical_school = resolved or raw_school  # keep raw as last-resort fallback

                db.add(HRStaffCache(
                    email=email,
                    name=name,
                    position=r.get("position", ""),
                    school=canonical_school,
                    classification=r.get("classification", ""),
                    tab_source=tab,
                    notes=(r.get("notes") or "").strip() or None,
                    cert_number=(r.get("cert_number") or "").strip() or None,
                    cached_at=now,
                ))
                result["added"] += 1

            if unresolved_buildings:
                result["unresolved_school_values"] = unresolved_buildings
                for raw_val, count in unresolved_buildings.items():
                    logger.warning(
                        f"HR sync: could not resolve school value {raw_val!r} "
                        f"({count} row{'s' if count != 1 else ''}) — add to "
                        f"branding.school_names or school_building_map"
                    )

            await db.commit()
            result["total"] = len(hr_records)
            logger.info(f"HR sync: {result}")

    except Exception as e:
        if not result["error"]:
            result["error"] = str(e)[:200]
        raise

    # Auto-queue provision/deprovision entries from HR diff
    try:
        async with AsyncSessionLocal() as db:
            queued = await _auto_queue_hr_diff(db)
            result["queued_provisions"] = queued.get("provisions", 0)
            result["queued_deprovisions"] = queued.get("deprovisions", 0)
            logger.info(f"HR auto-queue: {queued}")
    except Exception as e:
        logger.warning(f"HR auto-queue failed: {e}")

    # Trigger reconciliation with 30-second dedup window
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings
        settings = get_settings()
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        window = int(datetime.now(timezone.utc).timestamp() // 30)
        await redis.enqueue_job("run_staff_reconciliation", _job_id=f"reconciliation:{window}")
        logger.info("Enqueued staff reconciliation after HR sync")
    except Exception as e:
        logger.warning(f"Failed to enqueue reconciliation: {e}")

    return result


async def _auto_queue_hr_diff(db) -> dict:
    """
    Diff HR cache against staff_directory to find new/removed staff.
    Creates queue entries for provisioning or deprovisioning.
    Uses nickname-aware matching.
    """
    from app.modules.staff.models import HRStaffCache, StaffDirectoryEntry
    from app.modules.staff.nicknames import get_nickname_variants
    from app.modules.settings.repository import get_setting_value

    result = {"provisions": 0, "deprovisions": 0, "skipped": 0, "aborted_reason": None}

    # Load HR cache
    hr_rows = (await db.execute(select(HRStaffCache))).scalars().all()
    # Load staff directory (Google = source of truth). Include
    # suspended accounts so people on leave-of-absence are recognized
    # as already-existing and don't get queued for a bogus provision.
    # They still carry match_state from staff_sync, so HR-side matching
    # (nickname/alias/email) works the same way for them.
    dir_rows = (await db.execute(select(StaffDirectoryEntry))).scalars().all()

    # ── Safety gate: refuse if staff_directory looks mid-refresh ──
    # Root cause of the 2026-06-13 misfire (216 bogus provision rows,
    # cleaned up 2026-07-22): hr_sync ran while staff_sync had just
    # truncated staff_directory. dir_names was empty, every HR row scored
    # no-match, every HR row became a provision candidate.
    # Two-part gate:
    #   1. Absolute floor of 50 rows — even the smallest district ES has
    #      more than this in a healthy directory
    #   2. Ratio floor: directory ≥ 50% of HR cache. Directory should
    #      normally be ≥ HR since it also holds ex-staff / suspended;
    #      anything below half is a broken sync in flight.
    hr_count = len(hr_rows)
    dir_count = len(dir_rows)
    _MIN_DIR_ABSOLUTE = 50
    _MIN_DIR_RATIO = 0.5
    if hr_count and dir_count < _MIN_DIR_ABSOLUTE:
        msg = (
            f"staff_directory has {dir_count} rows (< {_MIN_DIR_ABSOLUTE} floor) — "
            f"refusing to run diff; probable mid-refresh"
        )
        logger.error("hr_sync provision gate: %s", msg)
        result["aborted_reason"] = msg
        return result
    if hr_count and dir_count < hr_count * _MIN_DIR_RATIO:
        msg = (
            f"staff_directory has {dir_count} rows, HR cache has {hr_count} "
            f"(< {_MIN_DIR_RATIO * 100:.0f}% ratio) — refusing to run diff; "
            f"probable mid-refresh"
        )
        logger.error("hr_sync provision gate: %s", msg)
        result["aborted_reason"] = msg
        return result

    # Build directory lookup: normalized name variants → email.
    # dir_names holds canonical "first last" tokens with:
    #   - nickname expansions of the first name (Bob/Robert/Rob...)
    #   - hyphen/compound splits of the last name ("White" and
    #     "Pennington-White" both resolvable when Google has one
    #     half and HR has the other)
    dir_names = set()
    for s in dir_rows:
        fn = (s.first_name or "").strip().lower()
        ln = (s.last_name or "").strip().lower()
        if fn and ln:
            first_variants = [fn] + list(get_nickname_variants(fn))
            for ln_v in _last_name_variants(ln):
                for fv in first_variants:
                    dir_names.add(f"{fv} {ln_v}")

    # dir_emails includes primary AND Google aliases. Alias data is
    # the only way to match maiden-name HR rows (e.g. HR has
    # "Jennifer Carver" but Google primary is jennifer.buffington@
    # with jennifer.carver@ as an alias).
    dir_emails = set()
    for s in dir_rows:
        if s.email:
            dir_emails.add(s.email.lower())
        raw_aliases = getattr(s, "google_aliases", None)
        if raw_aliases:
            try:
                for a in json.loads(raw_aliases):
                    if a:
                        dir_emails.add(str(a).lower())
            except (json.JSONDecodeError, TypeError):
                pass

    # Load existing queue entries to avoid duplicates
    existing_queue = set()
    rows = (await db.execute(text(
        "SELECT first_name, last_name, building, status FROM staff_queue "
        "WHERE status NOT IN ('complete', 'dismissed', 'rejected', 'completed')"
    ))).all()
    for r in rows:
        key = f"{(r[0] or '').lower()} {(r[1] or '').lower()}|{(r[2] or '').lower()}"
        existing_queue.add(key)

    domain = await get_setting_value(db, "google", "domain") or ""
    now = datetime.now(timezone.utc)

    # Find HR entries not in Google directory → provision
    for hr in hr_rows:
        hr_name = (hr.name or "").strip()
        if not hr_name:
            continue

        # Skip HR rows flagged as on leave. They look like new hires
        # to the matcher (no Google account yet, or suspended) but
        # provisioning them creates accounts that are immediately
        # suspended again when staff sync runs. Their Google account
        # is either already set up and parked, or intentionally
        # missing until their return date. Either way IT doesn't
        # want them in the provision queue.
        if is_on_leave(hr.notes):
            result["skipped"] += 1
            continue

        parts = hr_name.split(None, 1)
        if len(parts) < 2:
            continue
        first, last_raw = parts[0], parts[1]
        # HR staff frequently append suffixes ("Manchester, Jr.",
        # "Vest, III") to the last-name column. Those never appear in
        # Google, so strip them before comparing.
        last = _strip_suffix(last_raw)
        name_lower = f"{first} {last}".lower()

        # HR's email column is often a spreadsheet formula that
        # concatenates preferred-name + last-name + suffix, yielding
        # strings like "j.smith, jr.@example.edu".
        # Strip the suffix so we can compare against real Google
        # addresses.
        hr_email_raw = (hr.email or "").lower()
        hr_email = _strip_suffix(hr_email_raw)

        # Check if already in directory by (first × last) cross-product
        # of nickname variants and hyphen/compound splits.
        matched = False
        first_variants_hr = [first.lower()] + list(get_nickname_variants(first.lower()))
        last_variants_hr = _last_name_variants(last)
        # Maiden / former surname from HR Notes — "Née: Buffington"
        # → also try "jennifer buffington" so HR's current "Jennifer
        # Carver" matches Google's stale jennifer.buffington account.
        former_last = parse_former_last_name(hr.notes)
        if former_last:
            last_variants_hr = list(
                set(last_variants_hr) | set(_last_name_variants(former_last))
            )
        for fv in first_variants_hr:
            for lv in last_variants_hr:
                if f"{fv} {lv}" in dir_names:
                    matched = True
                    break
            if matched:
                break
        if not matched and hr_email and hr_email in dir_emails:
            matched = True
        # Last-resort: email local-part lookup. HR's computed email
        # may use a preferred name we haven't mapped as a nickname
        # (e.g. "Ronald" → "Buck"). If the local-part by itself
        # exists as any directory email, trust that.
        if not matched and hr_email and "@" in hr_email:
            local = hr_email.split("@", 1)[0]
            for de in dir_emails:
                if de.startswith(f"{local}@"):
                    matched = True
                    break

        if matched:
            continue

        # Check dedup
        building = (hr.school or "").strip().upper()
        dedup_key = f"{first.lower()} {last.lower()}|{building.lower()}"
        if dedup_key in existing_queue:
            result["skipped"] += 1
            continue

        # Generate expected email
        expected_email = ""
        if domain:
            fn_clean = re.sub(r"[^a-z]", "", first.lower())
            ln_clean = re.sub(r"[^a-z]", "", last.lower())
            if fn_clean and ln_clean:
                expected_email = f"{fn_clean}.{ln_clean}@{domain}"

        # Determine role_type from classification
        classification = (hr.classification or "").strip()
        role_type = "teacher"
        cl = classification.lower()
        if "class" in cl:
            role_type = "classified"
        elif "adm" in cl:
            role_type = "admin"
        elif "cert" in cl:
            role_type = "teacher"

        await db.execute(text("""
            INSERT INTO staff_queue
                (action, first_name, last_name, email, building, role_type, title,
                 source, status, position, classification, school,
                 expected_email, source_detail, created_at)
            VALUES
                ('provision', :first, :last, :email, :building, :role_type, :title,
                 'hr_sync', 'pending_data', :position, :classification, :school,
                 :expected_email, :source_detail, :ts)
        """).bindparams(
            first=first, last=last, email=hr_email or "", building=building,
            role_type=role_type, title=(hr.position or ""),
            position=(hr.position or ""), classification=classification,
            school=(hr.school or ""), expected_email=expected_email,
            source_detail=json.dumps({"tab": hr.tab_source or "", "hr_name": hr_name}),
            ts=now,
        ))
        existing_queue.add(dedup_key)
        result["provisions"] += 1

    # NOTE: Deprovision detection (HR entries removed since last sync) requires
    # comparing against a previous snapshot. Since we do full replace, we skip
    # deprovision auto-queuing for now — that requires a more sophisticated
    # "previous HR names" tracking mechanism. Manual deprovision via queue works.

    await db.commit()
    return result
