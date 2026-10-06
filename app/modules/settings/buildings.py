"""
Building code canonicalization.

Inside the app, **internal building codes** (PES, PHS, EPE, CO, DIST,
SUBS, …) are the canonical identifier everywhere — staff_directory,
staff_reconciliation, room_roster_cache, staff_queue, user_roles scope,
all UI filters, building notifications, badge printer routing.

External data sources speak different dialects:

    SIS codes         SIS_A, SIS_B, SIS_C (the district's SIS codes)
    Full school names "District High School", "East District Elementary"
    HR free-text      "District", "District - PES", "District/Bus Driver"
    Internal codes    PES, PHS, EPE, CO, …

`resolve_building_code()` is the single translator called at every
ingestion boundary (HR sync, Clever import, group sync, staff sync)
so downstream code can assume it only ever sees canonical internal
codes. No per-module translation logic, no SIS codes leaking past the
boundary, no duplicated lookups.

The canonical mapping sources are:

    branding.school_building_map    { SIS: internal }   e.g. { SIS_CODE: BLDG }
    branding.school_names           { SIS: full name }  e.g. { SIS_CODE: District Elementary School }

Together these define every known internal code the district uses.
Anything NOT mappable via these two sources falls through a set of
last-resort heuristics (substring search for a known internal code
or the word "district") and finally returns `None` so the caller can
log and skip.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# In-process cache so a sync job processing hundreds of rows doesn't
# hit the DB for every lookup. The maps rarely change — a short TTL
# is fine, and any change invalidates on restart anyway.
_CACHE: dict[str, Any] = {"loaded_at": 0.0, "sis_to_internal": {}, "name_to_internal": {}, "known_internal": set()}
_CACHE_TTL_SECONDS = 300  # 5 minutes


async def _load_maps(db) -> None:
    """Load school_building_map + school_names and build the lookup tables."""
    from app.modules.settings.repository import get_setting_value

    now = time.time()
    if _CACHE["loaded_at"] and (now - _CACHE["loaded_at"]) < _CACHE_TTL_SECONDS:
        return

    sis_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"

    try:
        sis_map = {k.strip().upper(): v.strip().upper() for k, v in json.loads(sis_raw).items()}
    except Exception as e:
        logger.warning(f"resolve_building_code: invalid school_building_map JSON: {e}")
        sis_map = {}
    try:
        names_raw_map = json.loads(names_raw)
    except Exception as e:
        logger.warning(f"resolve_building_code: invalid school_names JSON: {e}")
        names_raw_map = {}

    # Build name → internal by translating SIS-keyed school_names through
    # the SIS → internal map. Keys are lowercased/whitespace-collapsed
    # for forgiving comparison against free-text HR values.
    name_to_internal: dict[str, str] = {}
    for sis_key, full_name in names_raw_map.items():
        if not full_name:
            continue
        internal = sis_map.get(sis_key.strip().upper(), sis_key.strip().upper())
        norm = _normalize_name(full_name)
        if norm:
            name_to_internal[norm] = internal
        # Also register the name without the trailing "School" suffix
        # so HR's "East District Elementary" matches our stored
        # "East District Elementary School". Don't strip "elementary"
        # or "high" — those are part of the identifying name.
        short = re.sub(r"\s+school\s*$", "", full_name, flags=re.IGNORECASE).strip()
        short_norm = _normalize_name(short)
        if short_norm and short_norm not in name_to_internal:
            name_to_internal[short_norm] = internal

    # Set of known internal codes — anything in the right-hand side of
    # school_building_map, plus anything used as a bare SIS key that
    # didn't appear in the map (treated as already-internal).
    known_internal = set(sis_map.values())

    _CACHE["loaded_at"] = now
    _CACHE["sis_to_internal"] = sis_map
    _CACHE["name_to_internal"] = name_to_internal
    _CACHE["known_internal"] = known_internal


def _normalize_name(s: str | None) -> str:
    """Lower, collapse whitespace, strip punctuation. Used for name-match keys."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def invalidate_cache() -> None:
    """Call after a settings save if you need the cache refreshed immediately."""
    _CACHE["loaded_at"] = 0.0


# Non-district aliases with no mapping. Treated as pass-through codes
# so HR's free-text "SUBS" / "DIST" / "CO" etc. can be used directly.
_KNOWN_STANDALONE_CODES = {"CO", "DIST", "SUBS", "DISTRICT"}

# Phrase → code overrides, applied after the school_names lookup but
# before the fallthrough. Case-insensitive substring matching. Used
# for values that don't appear in school_names (like "Central Office"
# which isn't a school building) but are common enough in HR free-text
# to warrant a built-in shortcut.
_PHRASE_ALIASES = [
    ("central office", "CO"),
    ("co building", "CO"),
    ("substitute", "SUBS"),
    ("sub pool", "SUBS"),
]


async def resolve_building_code(raw: str | None, db) -> str | None:
    """
    Translate any incoming building reference into the canonical
    internal code. Returns None if unresolvable.

    Matching order:
        1. Blank input → None
        2. Exact internal code (e.g. "PES") → pass through uppercased
        3. Exact SIS code (e.g. "SIS_CODE") → internal via school_building_map
        4. Exact full school name (e.g. "District High School",
           case/punctuation-insensitive) → internal via school_names
        5. Standalone code (CO, DIST, SUBS, DISTRICT) → pass through
           (canonicalizing DISTRICT → DIST)
        6. Substring scan: first known internal code found as a whole
           word ("District - PES" → PES)
        7. Substring scan: contains "district" → DIST
        8. None, with an info log so the caller can spot unmapped values
    """
    if not raw:
        return None
    raw_clean = raw.strip()
    if not raw_clean:
        return None

    await _load_maps(db)

    sis_map = _CACHE["sis_to_internal"]
    name_map = _CACHE["name_to_internal"]
    known = _CACHE["known_internal"]

    upper = raw_clean.upper()

    # (2) Already an internal code
    if upper in known:
        return upper

    # (3) SIS code → internal
    if upper in sis_map:
        return sis_map[upper]

    # (4) Full school name
    norm = _normalize_name(raw_clean)
    if norm in name_map:
        return name_map[norm]

    # (4b) Phrase alias (Central Office, etc.)
    low = raw_clean.lower()
    for phrase, code in _PHRASE_ALIASES:
        if phrase in low:
            return code

    # (5) Standalone codes
    if upper in _KNOWN_STANDALONE_CODES:
        return "DIST" if upper == "DISTRICT" else upper

    # (6) Whole-word substring — "District - PES" → PES,
    #     "PES — Kitchen" → PES. Prefers internal codes over SIS.
    upper_tokens = re.findall(r"\b[A-Z]{2,6}\b", upper)
    for tok in upper_tokens:
        if tok in known:
            return tok
        if tok in sis_map:
            return sis_map[tok]
        if tok in _KNOWN_STANDALONE_CODES:
            return "DIST" if tok == "DISTRICT" else tok

    # (7) "District/Bus Driver" and similar → DIST
    if "district" in raw_clean.lower():
        return "DIST"

    # (8) Give up — log so the operator can add it to school_names or
    # school_building_map if it's a real case.
    logger.info(f"resolve_building_code: could not resolve {raw_clean!r}")
    return None


def resolve_building_code_sync(raw: str | None, maps: dict[str, Any]) -> str | None:
    """
    Synchronous version for callers that already have the maps loaded.

    `maps` should be a dict with the same shape as the module cache:
        {"sis_to_internal": {...}, "name_to_internal": {...}, "known_internal": set(...)}

    Useful for batch loops that resolve thousands of values without
    going through the async + cache path each time.
    """
    if not raw:
        return None
    raw_clean = raw.strip()
    if not raw_clean:
        return None

    sis_map = maps.get("sis_to_internal", {})
    name_map = maps.get("name_to_internal", {})
    known = maps.get("known_internal", set())

    upper = raw_clean.upper()
    if upper in known:
        return upper
    if upper in sis_map:
        return sis_map[upper]
    norm = _normalize_name(raw_clean)
    if norm in name_map:
        return name_map[norm]
    low = raw_clean.lower()
    for phrase, code in _PHRASE_ALIASES:
        if phrase in low:
            return code
    if upper in _KNOWN_STANDALONE_CODES:
        return "DIST" if upper == "DISTRICT" else upper
    for tok in re.findall(r"\b[A-Z]{2,6}\b", upper):
        if tok in known:
            return tok
        if tok in sis_map:
            return sis_map[tok]
        if tok in _KNOWN_STANDALONE_CODES:
            return "DIST" if tok == "DISTRICT" else tok
    if "district" in raw_clean.lower():
        return "DIST"
    return None


async def get_building_maps(db) -> dict[str, Any]:
    """
    Return the current resolver maps as a plain dict. Used by batch
    loops that want to resolve thousands of values via
    `resolve_building_code_sync()` without the async overhead per call.
    """
    await _load_maps(db)
    return {
        "sis_to_internal": dict(_CACHE["sis_to_internal"]),
        "name_to_internal": dict(_CACHE["name_to_internal"]),
        "known_internal": set(_CACHE["known_internal"]),
    }
