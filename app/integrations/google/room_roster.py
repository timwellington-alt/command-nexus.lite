"""
Room roster parsers — reads principal's room assignment sheets.

Districts have wildly different sheet formats. This module dispatches
to a per-format parser based on a `format` hint in settings:

    pes  — multi-set column layout: Room | Name | Assignment, merged
           floor headers, multi-name entries like "Smith/Jones".
    epe  — multi-set column layout: Room | Last | First, section
           headers interleaved between rows ("Kindergarten", "ED Rooms").
    phs  — extension directory: Name, Ext in "Last, First" format,
           three columns of name/ext pairs side by side. No rooms —
           the extension stands in as the locator.

All parsers output the same flat record shape for downstream consumers:
    {building, room, name, assignment, floor, is_esc}
"""

import logging
import re

logger = logging.getLogger(__name__)

# Names/entries to skip — not real staff assignments
SKIP_NAMES = {
    "", "conference", "empty", "storage", "stem lab", "science lab",
    "xtra speech", "xtra is", "xtra music", "trojan store",
    "ps eval", "principal's office",
}

# EPE section headers — text in col[0] that means "this row is a heading,
# not a room assignment". Not exhaustive; anything in col[0] that isn't
# numeric gets treated as a non-data row regardless.
EPE_SECTION_HEADERS = {
    "preschool", "kindergarten", "1st grade", "2nd grade", "3rd grade",
    "4th grade", "5th grade", "6th grade", "intervention teachers",
    "specials", "ed rooms", "md rooms", "speech", "office phone extensions",
    "paraprofessionals", "aides", "title", "reading", "math",
}

# PHS extension directory entries that are departments/services/shared
# lines, not individual staff. Matched case-insensitively as substrings.
PHS_NON_PERSON_KEYWORDS = (
    "cafeteria", "athletics", "band", "chorus", "print shop",
    "reference desk", "resource officer", "special ed desk",
    "tech support", "mental health", "counselors", "counsellors",
    "health", "clinic", "outside", "principal", "secretary",
    "conference", "front desk", "main office", "library",
    "xtra", "shared", "extra",
)


def parse_room_sheet(rows: list[list[str]], building: str, fmt: str = "pes") -> list[dict]:
    """
    Dispatch to a format-specific parser. `fmt` is the value of the
    building's `format` setting:

        nexus  — unified clean format from the NexusData tab template:
                 col0=Room/Ext, col1=Last, col2=First, col3=Category,
                 col4=Role/Grade/Outside. Header row is skipped.
        pes    — legacy multi-set "Room | Name | Assignment" layout
        epe    — legacy 3-col-per-set "Room | Last | First" layout
        phs    — legacy "Last, First" extension directory
    """
    f = (fmt or "pes").lower()
    if f == "nexus":
        return parse_room_sheet_nexus(rows, building)
    if f == "epe":
        return parse_room_sheet_epe(rows, building)
    if f == "phs":
        return parse_room_sheet_phs(rows, building)
    return parse_room_sheet_pes(rows, building)


def parse_room_sheet_nexus(rows: list[list[str]], building: str) -> list[dict]:
    """
    Parse the unified NexusData tab format. One row per person, fixed
    columns:

        col 0  Room or Extension (locator)
        col 1  Last Name
        col 2  First Name
        col 3  Category / Position (e.g. "Classroom Teacher", "Aide", "Teacher")
        col 4  Role / Grade / Outside number (context, optional)

    Header row is skipped. Empty rows are skipped. Rows missing first
    or last name are skipped.
    """
    if len(rows) < 2:
        return []

    results = []
    for row in rows[1:]:  # Skip header
        room = _cell(row, 0).strip()
        last = _cell(row, 1).strip()
        first = _cell(row, 2).strip()
        category = _cell(row, 3).strip()
        role = _cell(row, 4).strip()

        if not last or not first:
            continue
        if last.lower() in SKIP_NAMES or first.lower() in SKIP_NAMES:
            continue

        # Drop broken-formula cells (#REF!, #NAME?, #N/A, etc.) — they
        # leak through when a Data Entry lookup formula points at a
        # deleted / moved cell on the source spreadsheet. Left in, they
        # become an "#REF! #REF!" row in the provision queue.
        if last.startswith("#") or first.startswith("#"):
            continue

        # Strip trailing markers some sheets keep on names
        last = last.rstrip("*?").strip()
        first = first.rstrip("*?").strip()
        if not last or not first:
            continue

        assignment = role or category
        is_esc = bool(re.search(r"\bESC\b", f"{category} {role}", re.IGNORECASE))

        results.append({
            "building": building,
            # Locator may be missing for non-classroom staff — fall back
            # to a placeholder so the NOT NULL constraint is satisfied.
            "room": room or "?",
            "name": f"{first} {last}",
            "assignment": assignment,
            "floor": "",
            "is_esc": is_esc,
        })

    logger.info(f"Parsed {len(results)} unified-format records for {building}")
    return results


def parse_room_sheet_pes(rows: list[list[str]], building: str) -> list[dict]:
    """
    Parse a PES-format room assignment sheet into a flat list of
    room/name records.

    Returns list of:
        {building, room, name, assignment, floor, is_esc}
    """
    if len(rows) < 3:
        return []

    # Row 0: floor headers — detect how many column sets and their floor labels
    header_row = rows[0]
    col_headers = rows[1] if len(rows) > 1 else []

    # Detect column sets by finding "Rm" or "Room" in the header row
    sets = []
    for i, cell in enumerate(col_headers):
        if cell.strip().lower().startswith("rm") or cell.strip().lower().startswith("room"):
            # This is the start of a set (Room, Teacher/Name, Assignment)
            floor = ""
            # Look back in header_row for the floor label
            for j in range(i, -1, -1):
                if j < len(header_row) and header_row[j].strip():
                    floor = header_row[j].strip()
                    break
            sets.append({"start": i, "floor": floor})

    if not sets:
        # Fallback: assume 3-column sets starting at 0, 3, 6, 9...
        num_cols = max(len(r) for r in rows) if rows else 0
        for i in range(0, num_cols, 3):
            sets.append({"start": i, "floor": ""})

    logger.info(f"Detected {len(sets)} column sets for {building}")

    results = []
    for row in rows[2:]:  # Skip header rows
        for s in sets:
            base = s["start"]
            room = _cell(row, base).strip()
            name = _cell(row, base + 1).strip()
            assignment = _cell(row, base + 2).strip()

            if not room or not name:
                continue
            if name.lower() in SKIP_NAMES:
                continue

            # Handle multi-name entries (e.g. "Meyers/McGlone", "B. Smith/Harris")
            names = [n.strip() for n in name.split("/") if n.strip()]
            for n in names:
                if n.lower() in SKIP_NAMES:
                    continue
                # ESC = Educational Service Center contracted staff
                # Match "ESC" as a standalone word, not as part of "Preschool" etc.
                import re as _re
                is_esc = bool(_re.search(r'\bESC\b', assignment, _re.IGNORECASE))
                results.append({
                    "building": building,
                    "room": room,
                    "name": n,
                    "assignment": assignment,
                    "floor": s["floor"],
                    "is_esc": is_esc,
                })

    logger.info(f"Parsed {len(results)} room assignments for {building}")
    return results


def parse_room_sheet_epe(rows: list[list[str]], building: str) -> list[dict]:
    """
    Parse an EPE-format room assignment sheet.

    Layout:
        Row 0 is a title banner.
        Rows contain 3 column sets side by side. Each set is
        (room, last, first) with an empty separator column between
        sets. Column offsets observed: 0, 4, 8.
        Section headers (Preschool, Kindergarten, ED Rooms, etc.)
        appear interleaved where col[0] is a label instead of a
        room number; those rows are skipped per-set.
        First-name cells sometimes carry role hints like
        "Jennifer- Art" or "Jenny (Secretary)" — those are stripped.
    """
    if len(rows) < 2:
        return []

    # Column-set starting offsets. EPE uses 4-column spacing.
    SET_STARTS = (0, 4, 8)

    results = []
    # Track the most recent non-data label per set so sections like
    # "Preschool", "Intervention Teachers", and "Specials" attach to
    # the correct column instead of leaking across sets.
    current_section = {s: "" for s in SET_STARTS}

    for row in rows[1:]:  # Skip the title row
        # Detect section-header rows per-set: if col[start] is non-numeric
        # text, update that set's current section.
        for start in SET_STARTS:
            cell = _cell(row, start).strip()
            if cell and not cell[:1].isdigit():
                current_section[start] = cell

        for start in SET_STARTS:
            room = _cell(row, start).strip()
            last = _cell(row, start + 1).strip()
            first_raw = _cell(row, start + 2).strip()

            if not room or not last or not first_raw:
                continue
            # Room must look numeric (PHS-style 3-digit/4-digit room numbers)
            if not room[0].isdigit():
                continue
            # Skip known section headers that slipped through as col[0]
            if room.lower() in EPE_SECTION_HEADERS:
                continue
            if last.lower() in SKIP_NAMES:
                continue

            # Strip trailing role hints from the first name:
            #   "Jennifer- Art"     -> "Jennifer"
            #   "Jenny (Secretary)" -> "Jenny"
            #   "Matt "             -> "Matt"
            first = re.split(r"[-(]", first_raw, maxsplit=1)[0].strip()
            if not first:
                continue
            if first.lower() in SKIP_NAMES:
                continue

            role_hint = ""
            m = re.search(r"[-(](.+)", first_raw)
            if m:
                role_hint = m.group(1).strip().rstrip(")").strip()

            assignment = role_hint or current_section.get(start, "")
            is_esc = bool(re.search(r"\bESC\b", assignment, re.IGNORECASE))

            results.append({
                "building": building,
                "room": room,
                "name": f"{first} {last}",
                "assignment": assignment,
                "floor": "",
                "is_esc": is_esc,
            })

    logger.info(f"Parsed {len(results)} EPE room assignments for {building}")
    return results


def parse_room_sheet_phs(rows: list[list[str]], building: str) -> list[dict]:
    """
    Parse a PHS-format extension directory.

    Layout:
        Row 0 is header ("EMPLOYEE NAME", "EXT", ...).
        Data rows contain up to 3 name/ext column pairs:
            col 0: Name, col 1: Ext
            col 4: Name, col 5: Ext
            col 7: Name, col 8: Ext
        Name is in "Last, First" format, sometimes with trailing
        info like "Adkins, Ali - 355-4441" or "Armstrong, Mike - TAPS".
        Non-person entries (departments, services) are filtered by
        PHS_NON_PERSON_KEYWORDS and by absence of a comma.
    """
    if len(rows) < 2:
        return []

    # (name_col, ext_col) pairs — the empty separator columns are at 2, 3, 6
    PAIRS = ((0, 1), (4, 5), (7, 8))

    results = []
    for row in rows[1:]:  # Skip header row
        for name_col, ext_col in PAIRS:
            raw_name = _cell(row, name_col).strip()
            ext = _cell(row, ext_col).strip()

            if not raw_name:
                continue

            # Must be "Last, First" — no comma = department/service line
            if "," not in raw_name:
                continue

            low = raw_name.lower()
            if any(kw in low for kw in PHS_NON_PERSON_KEYWORDS):
                continue

            # Strip trailing " - 355-4441" or " - TAPS" hints before parsing
            name_part = re.split(r"\s+-\s+", raw_name, maxsplit=1)[0].strip()
            parts = name_part.split(",", 1)
            if len(parts) != 2:
                continue
            last = parts[0].strip()
            first = parts[1].strip()
            if not first or not last:
                continue
            if last.lower() in SKIP_NAMES or first.lower() in SKIP_NAMES:
                continue

            results.append({
                "building": building,
                # PHS has no room numbers — use the extension as the
                # locator so the NOT NULL constraint is satisfied and
                # the row is still self-describing.
                "room": ext or "?",
                "name": f"{first} {last}",
                "assignment": "",
                "floor": "",
                "is_esc": False,
            })

    logger.info(f"Parsed {len(results)} PHS directory entries for {building}")
    return results


def _cell(row: list, idx: int) -> str:
    """Safely get a cell value from a row."""
    if idx < len(row):
        return str(row[idx])
    return ""


async def sync_room_rosters(db) -> dict:
    """
    Pull all configured room roster sheets and cache the results.

    Reads building→sheet_id mapping from settings, fetches each sheet,
    parses room assignments, and replaces the cache.
    """
    from app.modules.settings.repository import get_setting_value
    from app.integrations.google.sheets_adapter import GoogleSheetsAdapter
    from sqlalchemy import text
    from datetime import datetime, timezone

    import json as _json

    buildings_raw = await get_setting_value(db, "room_roster", "buildings") or ""
    default_range = await get_setting_value(db, "room_roster", "sheet_range") or "Room Assignments!A1:Z60"

    building_sheets = _parse_building_sheets(buildings_raw, default_range)

    if not building_sheets:
        return {"error": "No building sheets configured", "synced": 0}

    # SIS code → internal code translation (e.g. SIS_C→EPE, SIS_B→PHS, SIS_A→PES).
    # The settings may store either kind — we always cache the internal code so
    # downstream matching (staff_directory.building) lines up.
    sis_map_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        sis_to_internal = {k.upper(): v.upper() for k, v in _json.loads(sis_map_raw).items()}
    except Exception:
        sis_to_internal = {}

    adapter = GoogleSheetsAdapter(db)
    now = datetime.now(timezone.utc)
    total = 0

    # Clear and rebuild
    await db.execute(text("DELETE FROM room_roster_cache"))

    for configured_code, cfg in building_sheets.items():
        # Per-building disable flag — set `disabled: true` on the entry
        # to stop pulling that sheet without deleting the config. Cache
        # rows for that building are cleared above by the DELETE and
        # will stay empty until re-enabled.
        if cfg.get("disabled") is True:
            logger.info(
                f"Room roster: {configured_code} SKIPPED (disabled flag set)"
            )
            continue
        sheet_id = cfg["sheet_id"]
        sheet_range = cfg["range"]
        fmt = cfg.get("format", "pes")
        # Translate to the internal building code when the user has
        # configured a SIS code in settings.
        building = sis_to_internal.get(configured_code.upper(), configured_code.upper())
        try:
            rows = await adapter.read_sheet(sheet_id, sheet_range)
            records = parse_room_sheet(rows, building, fmt=fmt)
            unresolved = 0
            for r in records:
                # Resolve facility_room_id at insert time. Null when the
                # (building, room) pair doesn't match facility_rooms —
                # surfaces drift immediately instead of at query time.
                await db.execute(text("""
                    INSERT INTO room_roster_cache
                        (building, room, name, assignment, floor, is_esc,
                         facility_room_id, cached_at)
                    VALUES (
                        :bldg, :room, :name, :assign, :floor, :esc,
                        (SELECT id FROM facility_rooms
                          WHERE building_code = :bldg AND room_code = :room
                          LIMIT 1),
                        :ts
                    )
                    RETURNING facility_room_id
                """).bindparams(
                    bldg=r["building"], room=r["room"], name=r["name"],
                    assign=r["assignment"], floor=r["floor"],
                    esc=r.get("is_esc", False), ts=now,
                ))
            total += len(records)
            # Detect any newly-inserted unresolved rows for this building
            # to surface drift in the logs (e.g. new sheet typo).
            unresolved_row = (await db.execute(text("""
                SELECT COUNT(*) FROM room_roster_cache
                WHERE building = :b AND facility_room_id IS NULL
                  AND cached_at = :ts
            """).bindparams(b=building, ts=now))).scalar()
            if unresolved_row:
                logger.warning(
                    f"Room roster: {building} — {unresolved_row} rows failed "
                    f"facility_room_id lookup (sheet typo or missing room). "
                    f"Query: SELECT building, room, name FROM room_roster_cache "
                    f"WHERE building='{building}' AND facility_room_id IS NULL;"
                )
            logger.info(
                f"Room roster: {configured_code} -> {building} ({fmt}) — "
                f"{len(records)} records from sheet {sheet_id[:12]}..."
            )
        except Exception as e:
            logger.error(f"Room roster sync failed for {configured_code} ({fmt}): {e}")

    await db.flush()

    # Auto-queue provisions from roster diff. Rosters are a valid
    # independent provision source — principals often add someone to
    # their room sheet before HR has processed the paperwork. The diff
    # function is guarded against noise: suffix-stripped, hyphen/nickname-
    # aware, and skips names already in HR so HR remains the primary
    # queueing path when both sources agree.
    queued = {"provisions": 0, "skipped": 0}
    try:
        queued = await _auto_queue_roster_diff(db)
        logger.info(f"Room roster auto-queue: {queued}")
    except Exception as e:
        logger.warning(f"Room roster auto-queue failed: {e}")

    # Regenerate the human-facing pretty view (Room #s tab) from
    # NexusData for any building configured with a layout output. Each
    # building's config can carry `layout_template_tab` +
    # `layout_output_tab` — presence of both triggers the regen. The
    # sync is the authoritative moment: every time we read NexusData
    # to update Nexus, we also push the derived view so admins never
    # see stale data on the sheet they look at day-to-day.
    layout_results: list[dict] = []
    for configured_code, cfg in building_sheets.items():
        if cfg.get("disabled") is True:
            continue
        template = (cfg.get("layout_template_tab") or "").strip()
        output = (cfg.get("layout_output_tab") or "").strip()
        if not template or not output:
            continue
        try:
            from app.integrations.google.room_layout import regenerate_layout
            summary = await regenerate_layout(
                db,
                sheet_id=cfg["sheet_id"],
                source_range=cfg.get("range") or "NexusData!A1:F200",
                template_tab=template,
                target_tab=output,
                section_columns=cfg.get("section_columns"),
                preserve_target=cfg.get("preserve_target", False),
                format_samples=cfg.get("format_samples"),
                footer_note=cfg.get("footer_note"),
            )
            summary["building"] = configured_code
            layout_results.append(summary)
            logger.info(
                f"Room layout regen for {configured_code}: "
                f"wrote {summary['rows_written']} rows to {output!r}"
            )
        except Exception as e:
            logger.warning(
                f"Room layout regen failed for {configured_code}: {e}"
            )

    return {
        "synced": total,
        "buildings": list(building_sheets.keys()),
        "queued_provisions": queued.get("provisions", 0),
        "layouts_regenerated": len(layout_results),
        "layout_results": layout_results,
    }


def _parse_building_sheets(raw: str, default_range: str) -> dict:
    """
    Parse the room_roster.buildings setting.

    Preferred format is JSON:
        {"PES": {"sheet_id": "...", "range": "Room Assignments!A1:Z60"}, ...}

    Legacy formats also accepted:
        - Comma- or newline-separated 'CODE=sheet_id' entries
          (range falls back to the default).
    """
    import json as _json

    if not raw:
        return {}

    raw = raw.strip()

    # Try JSON first
    if raw.startswith("{"):
        try:
            data = _json.loads(raw)
            out = {}
            for code, cfg in (data or {}).items():
                if not isinstance(cfg, dict):
                    continue
                sheet_id = (cfg.get("sheet_id") or "").strip()
                if not sheet_id:
                    continue
                bldg_range = (cfg.get("range") or "").strip() or default_range
                fmt = (cfg.get("format") or "pes").strip().lower() or "pes"
                # Optional layout regen config — presence of both keys
                # triggers a Room #s (Auto)-style pretty-view rebuild
                # from NexusData at the end of each roster sync.
                layout_template = (cfg.get("layout_template_tab") or "").strip()
                layout_output = (cfg.get("layout_output_tab") or "").strip()
                # Optional per-building layout tuning:
                #   section_columns — override which column each header
                #     lands in on the pretty view (EPE puts all grades
                #     in col 1; SIS_A splits K–3 / 4–6 across 1 & 2).
                #   preserve_target — skip the destructive clone step
                #     during regen. Use when the target tab has a
                #     protection that must not be lost.
                sec_cols = cfg.get("section_columns") or None
                if sec_cols and isinstance(sec_cols, dict):
                    sec_cols = {str(k): int(v) for k, v in sec_cols.items()}
                else:
                    sec_cols = None
                preserve = bool(cfg.get("preserve_target", False))
                # Per-building sample coord overrides for the formatter
                # (see apply_room_layout_format). Any of these keys are
                # optional; missing ones fall back to the (0,0)/(2,x)
                # defaults tuned for SIS_A's Room #s tab.
                fmt_samples = {}
                for k in ("header_sample", "data_sample_center",
                          "data_sample_right", "data_sample_name"):
                    v = cfg.get(k)
                    if v and isinstance(v, (list, tuple)) and len(v) == 2:
                        fmt_samples[k] = (int(v[0]), int(v[1]))
                # Footer note config — {text, cell?, span?}. Cell defaults
                # to A(N+2) where N is the rendered row count.
                footer = cfg.get("footer_note")
                if footer and not isinstance(footer, dict):
                    footer = None
                out[code.strip().upper()] = {
                    "sheet_id": sheet_id,
                    "range": bldg_range,
                    "format": fmt,
                    "layout_template_tab": layout_template,
                    "layout_output_tab": layout_output,
                    "section_columns": sec_cols,
                    "preserve_target": preserve,
                    "format_samples": fmt_samples or None,
                    "footer_note": footer,
                    "disabled": bool(cfg.get("disabled", False)),
                    "disabled_reason": cfg.get("disabled_reason") or "",
                }
            return out
        except Exception as e:
            logger.warning(f"Could not parse room_roster.buildings JSON: {e}")
            return {}

    # Legacy CODE=sheet_id format (comma or newline separated)
    out = {}
    for entry in raw.replace("\n", ",").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        code, sheet_id = entry.split("=", 1)
        code = code.strip().upper()
        sheet_id = sheet_id.strip()
        if code and sheet_id:
            out[code] = {"sheet_id": sheet_id, "range": default_range, "format": "pes"}
    return out


async def _auto_queue_roster_diff(db) -> dict:
    """
    Diff room_roster_cache against Google + HR, queue the leftovers.

    HR + each building's room roster are **peer** provisioning sources
    — the roster is authoritative for anyone HR doesn't see (notably
    ESC contractors, who never appear on the HR sheet). Escape hatches
    that remain are all identity-based, not role-based:

    1. **Skip if in Google.** Use the shared nickname/hyphen/alias
       matching (same helpers HR diff uses) so compound last names and
       preferred-name accounts are recognized.
    2. **Skip if in HR.** If the roster name matches an HR entry, HR
       sync owns the queueing decision — we don't want two queue rows
       for the same person with conflicting buildings.
    3. **Skip if already in staff_queue** (dedup — same first + last +
       building already pending).

    Single-name roster entries ("Smith" with no first name) are always
    skipped — we can't queue without a first name for expected_email
    generation and it's usually a last-name-only shorthand for someone
    already in Google.

    Historical note: this function used to also skip "support roles"
    (aide/para/ESC/speech/etc.) whenever multiple people shared a
    room, on the theory that principals sometimes park a rotating slot
    name in a room. That heuristic silently dropped real hires like
    Beth Arthurs (PES para) whose room happened to have 3 people —
    removed 2026-08-13 after Tim confirmed the 4 sources (HR + 3
    building rosters) are on the same plane and every named person
    should queue on merit, not role.
    """
    from sqlalchemy import select, text
    from app.modules.staff.models import StaffDirectoryEntry, HRStaffCache
    from app.modules.staff.nicknames import get_nickname_variants
    from app.modules.settings.repository import get_setting_value
    from app.workers.hr_sync_job import _strip_suffix, _last_name_variants
    import difflib
    import json
    import re as _re
    from datetime import datetime, timezone

    def _clean_token(s: str) -> str:
        """Strip stray punctuation the room sheet parser leaves behind."""
        return _re.sub(r"[^a-z0-9\-']+", "", s.lower()).strip("-'")

    result = {"provisions": 0, "skipped": 0}

    roster_rows = (await db.execute(text(
        "SELECT building, room, name, assignment, floor, is_esc FROM room_roster_cache"
    ))).all()

    # ── Google directory lookup (active + suspended) ──
    dir_rows = (await db.execute(select(StaffDirectoryEntry))).scalars().all()
    dir_names: set[str] = set()
    dir_last_only: set[str] = set()
    dir_emails: set[str] = set()
    for s in dir_rows:
        fn = _clean_token(s.first_name or "")
        ln = _clean_token(s.last_name or "")
        if fn and ln:
            first_variants = [fn] + list(get_nickname_variants(fn))
            for ln_v in _last_name_variants(ln):
                for fv in first_variants:
                    dir_names.add(f"{fv} {ln_v}")
                dir_last_only.add(ln_v)
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
    # Fuzzy-match list for edit-distance matches
    # (Kelly/Kelli, Jeanie/Jeannie, Rachel/Rachael, etc.)
    dir_names_list = list(dir_names)

    # ── HR name lookup (so HR-matched names fall to HR's auto-queue) ──
    hr_rows_db = (await db.execute(select(HRStaffCache))).scalars().all()
    hr_names: set[str] = set()
    for h in hr_rows_db:
        parts = (h.name or "").strip().split(None, 1)
        if len(parts) < 2:
            continue
        fn = _clean_token(parts[0])
        ln = _clean_token(_strip_suffix(parts[1].lower()))
        first_variants = [fn] + list(get_nickname_variants(fn))
        for ln_v in _last_name_variants(ln):
            for fv in first_variants:
                hr_names.add(f"{fv} {ln_v}")
    hr_names_list = list(hr_names)

    # ── Dedup against existing queue entries (active statuses only) ──
    # Normalize DB values through _clean_token + _strip_suffix so the
    # lookup key matches how NEW candidates are keyed further down.
    # Without this, "Libby St. Onge" in the DB (with period + space)
    # never matches "libby stonge" from the cleaner → infinite dupes.
    existing_queue = set()
    q_rows = (await db.execute(text(
        "SELECT first_name, last_name, building FROM staff_queue "
        "WHERE status NOT IN ('complete', 'completed', 'dismissed', 'rejected', 'failed')"
    ))).all()
    for r in q_rows:
        q_first = _clean_token(r[0] or "")
        q_last = _clean_token(_strip_suffix((r[1] or "").lower()))
        q_bldg = (r[2] or "").lower()
        if q_first and q_last:
            existing_queue.add(f"{q_first} {q_last}|{q_bldg}")

    domain = await get_setting_value(db, "google", "domain") or ""
    now = datetime.now(timezone.utc)

    for r in roster_rows:
        building, room, name, assignment, floor, is_esc = r[0], r[1], r[2], r[3], r[4], r[5]
        name = (name or "").strip()
        if not name:
            continue

        # Single-name entries ("Smith") are usually shorthand for
        # someone already in Google. If the last name matches any
        # directory last-name variant, treat as matched; otherwise
        # skip — we can't queue without a first name.
        name_parts = name.split()
        if len(name_parts) == 1:
            if name_parts[0].lower() in dir_last_only:
                continue
            result["skipped"] += 1
            continue

        # "F. Last" → first initial; still need a full first name to
        # queue, so skip. If the last name is in the directory, treat
        # as confirmed.
        first_raw = name_parts[0].rstrip(".")
        last_raw = " ".join(name_parts[1:])
        if len(first_raw) <= 1 or first_raw.endswith("."):
            if last_raw.lower() in dir_last_only:
                continue
            result["skipped"] += 1
            continue

        first = _clean_token(first_raw)
        last = _clean_token(_strip_suffix(last_raw.lower()))
        if not first or not last:
            result["skipped"] += 1
            continue

        # Cross-product match against Google directory — nickname variants
        # for first, hyphen/compound splits for last.
        # Head-token fallback: when the roster carries a compound first
        # name ("Bobbi Jo"), Google usually only stores the first token
        # ("Bobbi"). _clean_token strips spaces so "Bobbi Jo" collapses
        # to "bobbijo" — split off the head from the pre-clean raw so
        # the boundary survives.
        first_variants = [first] + list(get_nickname_variants(first))
        first_head_raw = first_raw.split(None, 1)[0] if " " in first_raw.strip() else ""
        first_head = _clean_token(first_head_raw) if first_head_raw else ""
        if first_head and first_head != first:
            first_variants.append(first_head)
            first_variants.extend(get_nickname_variants(first_head))
        last_variants = _last_name_variants(last)
        matched_google = any(
            f"{fv} {lv}" in dir_names
            for fv in first_variants
            for lv in last_variants
        )
        # Swapped-name check: some roster cells are "Last First" format
        # (e.g. "Eldridge Deanna"). If the straight order misses, try
        # interpreting the tokens as swapped and match again. Head-token
        # fallback applies here too — a swapped "Hobbs Bobbi Jo" turns
        # into first="Bobbi Jo" which needs to reduce to "Bobbi" to
        # match the "Bobbi Hobbs" directory entry. Split off the head
        # from the pre-clean raw last (which retains the space).
        if not matched_google:
            swapped_first_variants = [last] + list(get_nickname_variants(last))
            sw_head_raw = last_raw.split(None, 1)[0] if " " in last_raw.strip() else ""
            sw_head = _clean_token(sw_head_raw) if sw_head_raw else ""
            if sw_head and sw_head != last:
                swapped_first_variants.append(sw_head)
                swapped_first_variants.extend(get_nickname_variants(sw_head))
            swapped_last_variants = _last_name_variants(first)
            if any(
                f"{fv} {lv}" in dir_names
                for fv in swapped_first_variants
                for lv in swapped_last_variants
            ):
                matched_google = True
        # Fuzzy fallback — edit-distance match for spelling variants
        # like Kelly/Kelli, Jeanie/Jeannie, Rachel/Rachael. 0.88 cutoff
        # mirrors staff_sync_job's HR matcher.
        if not matched_google and difflib.get_close_matches(
            f"{first} {last}", dir_names_list, n=1, cutoff=0.88,
        ):
            matched_google = True
        if matched_google:
            continue

        # Skip if HR already has them — HR sync owns the queue row
        matched_hr = any(
            f"{fv} {lv}" in hr_names
            for fv in first_variants
            for lv in last_variants
        )
        if not matched_hr and difflib.get_close_matches(
            f"{first} {last}", hr_names_list, n=1, cutoff=0.88,
        ):
            matched_hr = True
        if matched_hr:
            result["skipped"] += 1
            continue

        # Dedup against existing queue (same name + building)
        dedup_key = f"{first} {last}|{building.lower()}"
        if dedup_key in existing_queue:
            result["skipped"] += 1
            continue

        # Generate expected email from clean parts
        expected_email = ""
        if domain:
            fn_clean = _re.sub(r"[^a-z]", "", first)
            ln_clean = _re.sub(r"[^a-z]", "", last)
            if fn_clean and ln_clean:
                expected_email = f"{fn_clean}.{ln_clean}@{domain}"

        await db.execute(text("""
            INSERT INTO staff_queue
                (action, first_name, last_name, building, role_type,
                 source, status, room, school,
                 expected_email, source_detail, title, created_at)
            VALUES
                ('provision', :first, :last, :building, 'teacher',
                 'room_roster', 'pending_data', :room, :building,
                 :expected_email, :source_detail, :assignment, :ts)
        """).bindparams(
            first=first_raw, last=last_raw, building=building,
            room=room, expected_email=expected_email,
            source_detail=json.dumps({"floor": floor, "assignment": assignment, "is_esc": is_esc}),
            assignment=assignment or "", ts=now,
        ))
        existing_queue.add(dedup_key)
        result["provisions"] += 1

    await db.commit()
    return result
