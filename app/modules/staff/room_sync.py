"""
Room-to-extension cross-reference.

Compares room roster data against UCM phone extension cache
to find caller ID name mismatches. When a staff member moves rooms,
the extension in that room needs its caller ID updated.

Uses the building_map setting to derive extension prefixes per building.
"""

import logging
import re
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def find_extension_mismatches(db: AsyncSession) -> list[dict]:
    """
    Cross-reference room roster against phone extensions.

    For each room roster entry:
    1. Derive the expected extension from building prefix + room number
    2. Look up that extension in the phone cache
    3. If the caller ID name doesn't match the room roster name → flag it

    Returns list of mismatch dicts.
    """
    import json
    from app.modules.settings.repository import get_setting_value

    # Parse building_map to get building → prefix mapping
    bmap_raw = await get_setting_value(db, "grandstream", "building_map") or ""
    prefix_map = _parse_building_prefixes(bmap_raw)

    if not prefix_map:
        return []

    # Load room roster
    rooms = await db.execute(text(
        "SELECT building, room, name, assignment, floor, is_esc FROM room_roster_cache ORDER BY building, room"
    ))
    room_entries = rooms.all()

    # Load phone extensions
    exts = await db.execute(text(
        "SELECT extension, caller_id_name FROM phone_extension_cache"
    ))
    ext_map = {r[0]: r[1] for r in exts.all()}

    # Also load SIS → internal building map for translation
    sis_map_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        sis_map = json.loads(sis_map_raw)
    except Exception:
        sis_map = {}
    # Build reverse: internal → SIS
    reverse_map = {}
    for sis, internal in sis_map.items():
        reverse_map[internal.upper()] = sis.upper()

    mismatches = []

    # Support roles that share rooms — they don't have their own extension
    skip_assignments = {"is", "md", "esc", "aide", "psych", "speech", "literacy coach", "counseling"}

    for building, room, name, assignment, floor, is_esc in room_entries:
        # Skip non-person entries
        if not name or not room:
            continue

        # Skip support staff who share rooms — the extension belongs to the lead teacher
        assign_lower = (assignment or "").lower()
        if is_esc or any(skip in assign_lower for skip in skip_assignments):
            continue

        # Get the extension prefix for this building
        bldg_upper = building.upper()
        prefix = prefix_map.get(bldg_upper)
        if not prefix:
            continue

        # Calculate expected extension: prefix + room number
        expected_ext = f"{prefix}{room}"

        # Look up what's on that extension
        current_name = ext_map.get(expected_ext)
        if current_name is None:
            # Extension doesn't exist in cache — skip
            continue

        # Compare names — match against last name (room roster often has last name only)
        roster_last = name.strip()
        current_clean = (current_name or "").strip()

        if not current_clean or not roster_last:
            continue

        # Check if current extension name contains the roster last name
        if _names_match(roster_last, current_clean):
            continue

        # Mismatch found
        mismatches.append({
            "building": building,
            "room": room,
            "floor": floor,
            "roster_name": roster_last,
            "expected_ext": expected_ext,
            "current_ext_name": current_clean,
            "assignment": assignment,
            "is_esc": is_esc,
        })

    logger.info(f"Room-extension cross-reference: {len(mismatches)} mismatches found")
    return mismatches


def _names_match(roster_name: str, ext_name: str) -> bool:
    """
    Check if the roster name matches the extension caller ID name.

    Roster has last name only (e.g. "Kolar", "B. Smith").
    Extension has full name (e.g. "Jill Kolar", "B Smith").

    Match if the roster last name appears in the extension name.
    """
    roster_lower = roster_name.lower().strip().rstrip("?")
    ext_lower = ext_name.lower().strip()

    # Direct containment — "Kolar" in "Jill Kolar"
    if roster_lower in ext_lower:
        return True

    # Handle initials — "B. Smith" should match "B Smith"
    roster_clean = roster_lower.replace(".", "").replace(" ", "")
    ext_clean = ext_lower.replace(".", "").replace(" ", "")
    if roster_clean in ext_clean:
        return True

    # Match last word of roster against last word of extension
    roster_parts = roster_lower.split()
    ext_parts = ext_lower.split()
    if roster_parts and ext_parts:
        if roster_parts[-1] == ext_parts[-1]:
            return True

    return False


def _parse_building_prefixes(building_map: str) -> dict[str, str]:
    """
    Extract building → extension prefix from the phone building_map setting.

    Setting format: "2000-2199=PES 1st Floor,2200-2299=PES 2nd Floor,..."

    Returns {building_code: prefix_digit} e.g. {"PES": "2", "EPE": "3", "PHS": "1"}
    """
    prefixes = {}
    if not building_map:
        return prefixes

    for entry in building_map.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        range_part, label = entry.split("=", 1)
        range_part = range_part.strip()
        label = label.strip()

        # Extract building code from label (first word, e.g. "PES" from "PES 1st Floor")
        bldg = label.split()[0].upper() if label else ""
        if not bldg:
            continue

        # Extract prefix from range start (e.g. "2000" → "2")
        range_start = range_part.split("-")[0].strip()
        if range_start and range_start[0].isdigit():
            prefix = range_start[0]
            # Only set if not already set (first range defines the prefix)
            if bldg not in prefixes:
                prefixes[bldg] = prefix

    return prefixes
