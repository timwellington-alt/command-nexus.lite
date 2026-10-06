"""
NexusData → Room #s pretty-layout converter.

Reads a building's flat NexusData tab (Room, Last, First, Category,
Role/Grade) and writes the human-friendly multi-column category-
grouped Room #s layout to a target tab. Used by the room-roster sync
job so admin-facing sheets always reflect the current NexusData
without a manual re-copy step.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict


logger = logging.getLogger(__name__)


# Category → display header on the pretty layout. Keys are the
# NexusData "Category" values (with special-case for Classroom Teacher
# where the header is actually the Role/Grade). Value ``None`` means
# "use the Role/Grade instead" for the header text.
CATEGORY_HEADERS = {
    "Classroom Teacher": None,
    "Intervention Teacher": "Intervention Teachers",
    "Resource Room": "Resource Rooms",
    "School Psychologist": "School Psychologist",
    "Literacy Coordinator": "Literacy Coordinator",
    "ESL": "ESL",
    "Kitchen": "Kitchen",
    "Speech": "Speech",
    "Specials": "Specials",
    "Office": "Office Phone Extensions",
    "Paraprofessional": "Paraprofessionals",
    "Miscellaneous": "Miscellaneous",
}

GRADE_ORDER = [
    "Preschool", "Kindergarten",
    "1st Grade", "2nd Grade", "3rd Grade",
    "4th Grade", "5th Grade", "6th Grade",
]

# Section → visual column. Reflects the current human-arranged layout
# in Room #s. Unlisted sections default to column 3.
SECTION_COLUMN = {
    "Preschool": 1, "Kindergarten": 1,
    "1st Grade": 1, "2nd Grade": 1, "3rd Grade": 1,
    "Miscellaneous": 1,
    "4th Grade": 2, "5th Grade": 2, "6th Grade": 2,
    "Intervention Teachers": 2, "Resource Rooms": 2,
    "School Psychologist": 2, "Literacy Coordinator": 2,
    "Kitchen": 2, "ESL": 2,
    "Speech": 3, "Specials": 3,
    "Office Phone Extensions": 3, "Paraprofessionals": 3,
}


def _strip_grade_suffix(last: str) -> str:
    """Strip patterns like " - 6" or " - K" from a last name — leftover
    from the pretty view leaking back into NexusData's Last Name column."""
    s = (last or "").strip()
    return re.sub(r"\s*-\s*(K|\d+)\s*$", "", s)


def _grade_suffix_for_intervention(role: str) -> str:
    """Compact grade suffix ("K", "2", "6") from a Role/Grade string
    for the pretty view's Intervention rows ("Blevins - 2")."""
    r = (role or "").strip().lower()
    if not r:
        return ""
    if "kinder" in r:
        return "K"
    m = re.match(r"(\d+)", r)
    return m.group(1) if m else ""


def build_sections(rows: list[list[str]]) -> dict[str, list[list[str]]]:
    """
    Group NexusData rows into pretty-layout sections. Returns
    ``{ section_header: [ [room, last, first, sub, highlight], ... ] }``.
    Preserves insertion order within each section. ``highlight`` is
    ``""`` for normal rows or ``"loa"``/``"sub"`` for LOA teachers /
    long-term subs merged into their grade section — the layout
    formatter uses that marker to paint a background color.
    """
    sections: dict[str, list[list[str]]] = defaultdict(list)

    for r in rows[1:]:  # skip header row
        if not r:
            continue
        room = (r[0] if len(r) > 0 else "").strip()
        last = (r[1] if len(r) > 1 else "").strip()
        first = (r[2] if len(r) > 2 else "").strip()
        category = (r[3] if len(r) > 3 else "").strip()
        role = (r[4] if len(r) > 4 else "").strip()

        # Skip broken formulas and empty stubs.
        if any(s.startswith("#REF") for s in (room, last, first, category, role)):
            continue
        if not room and not last and not first:
            continue
        if not category:
            continue

        highlight = ""

        if category == "Classroom Teacher":
            header = role or "Classroom Teacher"
            sub = ""
            last_out = _strip_grade_suffix(last)
        elif category == "Intervention Teacher":
            header = "Intervention Teachers"
            suffix = _grade_suffix_for_intervention(role)
            base = _strip_grade_suffix(last)
            last_out = f"{base} - {suffix}" if suffix and base else base
            sub = ""
        elif category == "LOA":
            # LOA teachers stay visible in their grade section so the
            # roster shows the room's normal assignment. The long-term
            # sub covering the room appears alongside via Teacher-Sub.
            header = role or "LOA"
            last_out = _strip_grade_suffix(last)
            sub = "LOA"
            highlight = "loa"
        elif category == "Teacher-Sub":
            # Long-term subs render in the grade section for the room
            # they cover, next to the LOA teacher they're filling for.
            header = role or "Teacher-Sub"
            last_out = _strip_grade_suffix(last)
            sub = "Sub"
            highlight = "sub"
        elif category in CATEGORY_HEADERS:
            header = CATEGORY_HEADERS[category] or category
            last_out = _strip_grade_suffix(last)
            sub = role
        else:
            header = category
            last_out = _strip_grade_suffix(last)
            sub = role

        sections[header].append([room, last_out, first, sub, highlight])

    return sections


def render_layout(
    sections: dict[str, list[list[str]]],
    section_columns: dict[str, int] | None = None,
) -> tuple[list[list[str]], list[dict]]:
    """
    Assemble the 12-column layout: three side-by-side blocks of four
    columns each. Returns ``(grid, cell_meta)`` where ``cell_meta`` is
    a flat list of ``{"row": <0-idx>, "block": <1|2|3>, "kind":
    "header"|"spacer"|"data"|"empty"}`` — consumed by the formatter to
    know which rows are section headers (merged, gray, centered) vs
    data rows (borders, name alignment) per block.

    ``section_columns`` — optional per-building override of SECTION_COLUMN.
    Different schools arrange their printable directories differently
    (EPE puts all grades in col 1; SIS_A splits K–3rd and 4th–6th across
    cols 1 and 2). Pass the building's mapping through to keep the auto
    tab visually identical to whatever the human template uses.
    """
    col_map = section_columns or SECTION_COLUMN
    columns: dict[int, list[tuple[str, list[str]]]] = {1: [], 2: [], 3: []}

    ordered: list[str] = []
    for g in GRADE_ORDER:
        if g in sections:
            ordered.append(g)
    for header in col_map.keys():
        if header in sections and header not in ordered:
            ordered.append(header)
    for header in sections.keys():
        if header not in ordered:
            ordered.append(header)

    for header in ordered:
        col = col_map.get(header, 3)
        buf = columns[col]
        if buf:
            buf.append(("spacer", ["", "", "", ""], ""))
        buf.append(("header", [header, "", "", ""], ""))
        for entry in sections[header]:
            # entry = [room, last, first, sub, highlight?]. Cells are
            # the first 4; highlight (if present) is stashed in
            # cell_meta so the formatter can color the row.
            cells = entry[:4]
            highlight = entry[4] if len(entry) > 4 else ""
            buf.append(("data", cells, highlight))

    height = max(len(c) for c in columns.values()) if any(columns.values()) else 0
    grid: list[list[str]] = []
    cell_meta: list[dict] = []
    for i in range(height):
        row: list[str] = []
        for col_idx in (1, 2, 3):
            block = columns[col_idx]
            if i < len(block):
                kind, cells, highlight = block[i]
                row.extend(cells)
                cell_meta.append({
                    "row": i, "block": col_idx, "kind": kind,
                    "highlight": highlight,
                })
            else:
                row.extend(["", "", "", ""])
                cell_meta.append({
                    "row": i, "block": col_idx, "kind": "empty",
                    "highlight": "",
                })
        grid.append(row)
    return grid, cell_meta


async def regenerate_layout(
    db,
    sheet_id: str,
    source_range: str = "NexusData!A1:F200",
    template_tab: str = "Room #s",
    target_tab: str = "Room #s (Auto)",
    section_columns: dict[str, int] | None = None,
    preserve_target: bool = False,
    format_samples: dict | None = None,
    footer_note: dict | None = None,
) -> dict:
    """
    End-to-end pipeline: read NexusData, build sections, render grid,
    clone the template, write payload, re-apply merges + formatting.

    ``section_columns`` — optional per-building column mapping (see
    ``render_layout``). Defaults to the SIS_A-style SECTION_COLUMN.

    ``preserve_target`` — when True, skip cloning the template over the
    target tab. Use this when the target already exists with correct
    formatting AND has a protection that would be lost on re-clone
    (Directory (Auto) with an SA-only lock, for example). The
    formatting sampler still runs against ``template_tab`` so per-block
    fonts/colors stay in sync.

    Returns a summary dict. Wrapped in try/except by callers so a
    layout failure doesn't block whatever job scheduled it.
    """
    from app.integrations.google.sheets_adapter import GoogleSheetsAdapter

    a = GoogleSheetsAdapter(db)
    rows = await a.read_sheet(sheet_id, source_range)
    sections = build_sections(rows)
    grid, cell_meta = render_layout(sections, section_columns=section_columns)

    tab = target_tab.strip("'\"")
    write_range = f"'{tab}'!A1:L200"

    if not preserve_target:
        await a.clone_tab(
            sheet_id,
            source_title=template_tab,
            dest_title=tab,
            replace_if_exists=True,
        )
    await a.unmerge_tab(sheet_id, tab)
    await a.write_sheet(sheet_id, write_range, grid, clear_first=True)
    # Per-building sample coord overrides — a template's section-header
    # row may not be at (0,0). EPE's Sheet1 has a title at row 0 and
    # section headers at row 1, so sampler defaults would grab title
    # formatting for every section header. Callers pass explicit coords.
    fmt_kwargs = dict(format_samples or {})
    await a.apply_room_layout_format(
        sheet_id, tab, cell_meta, template_tab=template_tab, **fmt_kwargs,
    )

    # Optional footer note written after the layout — for schools that
    # print a phone-dialing rule ("dial 3 + room number") or similar
    # static instruction under the directory. Cell is an A1 reference
    # relative to the target tab; text may span merged cells via
    # ``span``.
    if footer_note and footer_note.get("text"):
        cell = footer_note.get("cell") or f"A{len(grid) + 2}"
        span = int(footer_note.get("span") or 1)
        await a.write_sheet(
            sheet_id, f"'{tab}'!{cell}", [[footer_note["text"]]],
            clear_first=False,
        )
        if span > 1:
            # Merge across ``span`` columns starting at ``cell`` so
            # long-note text wraps naturally instead of overflowing.
            await _merge_footer(a, sheet_id, tab, cell, span)

    return {
        "sheet_id": sheet_id,
        "target_tab": target_tab,
        "sections": {h: len(v) for h, v in sections.items()},
        "rows_written": len(grid),
    }


async def _merge_footer(a, sheet_id: str, tab: str, cell_a1: str, span: int) -> None:
    """Merge N columns starting at ``cell_a1`` on ``tab`` — small helper
    so the footer_note config can carry a ``span`` for long instructions
    without exposing the low-level batchUpdate shape to callers."""
    import re
    m = re.match(r"([A-Z]+)(\d+)", cell_a1.upper())
    if not m:
        return
    col_letters, row_str = m.group(1), m.group(2)
    col_idx = 0
    for ch in col_letters:
        col_idx = col_idx * 26 + (ord(ch) - 64)
    col_idx -= 1
    row_idx = int(row_str) - 1

    svc = await a._get_service(write=True)
    def _run():
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id, fields="sheets(properties(sheetId,title))",
        ).execute()
        sid = next(
            s["properties"]["sheetId"] for s in meta["sheets"]
            if s["properties"]["title"] == tab
        )
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={
            "requests": [{"mergeCells": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                    "startColumnIndex": col_idx, "endColumnIndex": col_idx + span,
                },
                "mergeType": "MERGE_ALL",
            }}],
        }).execute()
    import asyncio
    await asyncio.to_thread(_run)
