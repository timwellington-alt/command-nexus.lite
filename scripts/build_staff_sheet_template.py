#!/usr/bin/env python3
"""Build the Staff Directory + Phone Directory xlsx template.

Writes docs/templates/staff_directory_template.xlsx. Districts upload
this to Google Drive, convert to a Google Sheet, and paste the Sheet
ID into Nexus Settings → HR Google Sheets. That's the single staff
data source for the lite build.

Two tabs:
  1. "Staff Directory" — the import-friendly entry tab. One row per
     staff member. Column order matches what the hr_sync worker reads.
     Header row is bold with filter + freeze-pane applied.
  2. "Phone Directory" — a printable staff phone list. Pulls from the
     Staff Directory tab via FILTER() + SORT(). Formatted for 8.5x11
     portrait print with page header + repeating title row.

Rebuild via:
    python3 scripts/build_staff_sheet_template.py

openpyxl is a dev-only dep. Not in requirements.txt — operators never
need to rebuild this file; they consume the committed xlsx.
"""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "docs" / "templates" / "staff_directory_template.xlsx"


# ─── Staff Directory tab ──────────────────────────────────────────────
# Column order matches what app/workers/hr_sync_job.py reads via
# r.get(<col>). Operators can rename the TAB freely but columns are
# matched by header text so keep the exact strings in row 1.
STAFF_COLUMNS = [
    ("first_name",     "First Name",       14, "Given name — matches staff_directory.first_name"),
    ("last_name",      "Last Name",        16, "Family name"),
    ("preferred_name", "Preferred Name",   14, "Used on ID cards + the welcome page. Leave blank to default to First Name."),
    ("email",          "Email",            32, "Full district email (firstname.lastname@yourdistrict.org)"),
    ("position",       "Position / Title", 28, "Printed on the ID card. e.g. 'Grade 3 Teacher', 'Head Custodian'"),
    ("classification", "Classification",   14, "One of: Cert | Class | Adm | Adm-Class | xmpt (per reference_hr_classification_codes)"),
    ("school",         "Building",         20, "Must match a value in Settings → Branding → School → Building Map. Free text accepted; resolver handles common variants."),
    ("room",           "Room",             10, "Optional. Room number / code. Shown on staff profile."),
    ("extension",      "Phone Ext.",       10, "Internal phone extension (3-4 digits typically)"),
    ("cert_number",    "OH Cert #",        14, "Ohio Dept. of Education teacher cert number. Optional. Appears on cert reports."),
    ("notes",          "HR Notes",         40, "Free text. Nexus parses 'former last name: X' from here to reconcile post-marriage/divorce records. Any other text ignored."),
]


# ─── Phone Directory tab formulas ─────────────────────────────────────
# Pulls LastName, FirstName, Position, Building, Extension, Phone from
# the Staff Directory tab. Filters rows where email AND last_name are
# non-empty (keeps the directory free of half-filled stub rows). Sorts
# by Building then Last Name. Uses the Google-Sheets-compatible
# FILTER/SORT functions; also works in Excel 365 after conversion.
PHONE_COLUMNS = [
    ("Last Name",      14),
    ("First Name",     14),
    ("Position",       28),
    ("Building",       20),
    ("Ext.",            8),
]


def _header_style():
    return {
        "font": Font(bold=True, color="FFFFFF", size=11),
        "fill": PatternFill("solid", fgColor="2C3E50"),
        "alignment": Alignment(horizontal="left", vertical="center"),
    }


def _thin_border():
    thin = Side(border_style="thin", color="D0D4D8")
    return Border(left=thin, right=thin, top=thin, bottom=thin)


def _build_staff_tab(wb):
    ws = wb.active
    ws.title = "Staff Directory"
    ws.sheet_view.showGridLines = True

    headers = [h[1] for h in STAFF_COLUMNS]
    ws.append(headers)

    hs = _header_style()
    for col_idx, (_, _, width, hover) in enumerate(STAFF_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = hs["font"]
        cell.fill = hs["fill"]
        cell.alignment = hs["alignment"]
        cell.border = _thin_border()
        cell.comment = None  # Comments in openpyxl are a different object; skip for now
        letter = get_column_letter(col_idx)
        ws.column_dimensions[letter].width = width

    # Add a couple of example rows so operators see the shape expected
    examples = [
        ["Jane",  "Smith",   "", "jane.smith@yourdistrict.org",    "Grade 3 Teacher",   "Cert",     "Elementary",   "203",   "3203",  "123456", ""],
        ["John",  "Taylor",  "Jack", "john.taylor@yourdistrict.org", "Head Custodian",   "Class",    "High School",  "Shop",  "1500",  "",       ""],
        ["Maria", "Garcia",  "", "maria.garcia@yourdistrict.org",   "Assistant Superintendent", "Adm", "Central Office", "",   "100",   "",       "former last name: Lopez"],
    ]
    for row in examples:
        ws.append(row)

    # Example rows styled in italic so operators spot them vs. real data
    italic = Font(italic=True, color="808080")
    for r in range(2, 2 + len(examples)):
        for c in range(1, len(STAFF_COLUMNS) + 1):
            ws.cell(row=r, column=c).font = italic
            ws.cell(row=r, column=c).border = _thin_border()

    # Freeze header row + first two columns so names stay visible when
    # scrolling right.
    ws.freeze_panes = "C2"

    # Table (= auto-filter + banded rows). Range sized to a generous
    # pre-allocated 500 rows so new entries slot right in.
    table_range = f"A1:{get_column_letter(len(STAFF_COLUMNS))}500"
    # Pad empty rows so the table range isn't larger than the data —
    # Excel errors on tables with no cell content past header.
    current_rows = ws.max_row
    for _ in range(current_rows, 500):
        ws.append([""] * len(STAFF_COLUMNS))
    tab = Table(displayName="StaffDirectory", ref=table_range)
    tab.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2", showFirstColumn=False,
        showLastColumn=False, showRowStripes=True, showColumnStripes=False,
    )
    ws.add_table(tab)


def _build_phone_tab(wb):
    ws = wb.create_sheet("Phone Directory")
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.page_margins.left = 0.5
    ws.page_margins.right = 0.5
    ws.page_margins.top = 0.5
    ws.page_margins.bottom = 0.5
    ws.print_options.horizontalCentered = True
    ws.print_title_rows = "1:2"  # Repeat the title + header on each printed page

    # Row 1: title banner (merged across columns)
    ws.append(["Staff Phone Directory"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(PHONE_COLUMNS))
    title_cell = ws.cell(row=1, column=1)
    title_cell.font = Font(bold=True, size=18, color="2C3E50")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    # Row 2: column headers
    headers = [h[0] for h in PHONE_COLUMNS]
    ws.append(headers)
    hs = _header_style()
    for col_idx, (_, width) in enumerate(PHONE_COLUMNS, start=1):
        cell = ws.cell(row=2, column=col_idx)
        cell.font = hs["font"]
        cell.fill = hs["fill"]
        cell.alignment = hs["alignment"]
        cell.border = _thin_border()
        letter = get_column_letter(col_idx)
        ws.column_dimensions[letter].width = width

    # Row 3: single formula cell drives the whole table. Uses the
    # Google-Sheets syntax SORT(FILTER(...)) which also works in Excel
    # 365 as a dynamic array. Columns referenced by their heading
    # position in Staff Directory (not by name) so renaming headers
    # there breaks this formula — document that caveat.
    formula = (
        "=SORT("
        "FILTER("
        "{{'Staff Directory'!B2:B,'Staff Directory'!A2:A,'Staff Directory'!E2:E,'Staff Directory'!G2:G,'Staff Directory'!I2:I}},"
        "'Staff Directory'!D2:D<>\"\","
        "'Staff Directory'!B2:B<>\"\""
        "),"
        "4,TRUE,1,TRUE"
        ")"
    )
    ws.cell(row=3, column=1, value=formula)

    ws.freeze_panes = "A3"


def _build_readme_tab(wb):
    """A README tab explaining how to wire the sheet into Nexus."""
    ws = wb.create_sheet("README", 0)  # Insert as first tab
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 100

    lines = [
        ("Command Nexus — Staff Directory Template", 18, True, "2C3E50"),
        ("", 11, False, None),
        ("1. Upload this file to Google Drive and open it as a Google Sheet.", 11, False, None),
        ("   (Right-click in Drive → Open with → Google Sheets. Save as a Sheet.)", 11, False, None),
        ("", 11, False, None),
        ("2. Share the resulting Sheet with your Nexus service account email", 11, False, None),
        ("   (same account you pasted into secrets/google_service_account.json)", 11, False, None),
        ("   with Viewer access.", 11, False, None),
        ("", 11, False, None),
        ("3. Copy the Sheet ID from its URL:", 11, False, None),
        ("     https://docs.google.com/spreadsheets/d/XXXXXXXXXXX/edit", 11, False, None),
        ("                                       ^^^^^^^^^^^^", 11, False, None),
        ("", 11, False, None),
        ("4. Paste the Sheet ID into Nexus Settings → HR Google Sheets →", 11, False, None),
        ("   `sheet_id`. Nexus's HR sync job will poll the sheet nightly", 11, False, None),
        ("   and populate the staff directory.", 11, False, None),
        ("", 11, False, None),
        ("How this file is organized:", 13, True, None),
        ("", 11, False, None),
        ("• Staff Directory tab", 11, True, None),
        ("    Your HR team enters one row per staff member. Column headers in", 11, False, None),
        ("    row 1 are matched by Nexus's HR sync — don't rename them. The", 11, False, None),
        ("    three example rows below the header are styled gray/italic and", 11, False, None),
        ("    can be deleted or overwritten with real data.", 11, False, None),
        ("", 11, False, None),
        ("• Phone Directory tab", 11, True, None),
        ("    Printable 8.5×11 portrait view of staff phone extensions,", 11, False, None),
        ("    sorted by building then last name. Pulls automatically from the", 11, False, None),
        ("    Staff Directory tab via SORT(FILTER()). To print: File →", 11, False, None),
        ("    Print → Current sheet.", 11, False, None),
        ("", 11, False, None),
        ("Classification codes:", 13, True, None),
        ("", 11, False, None),
        ("    Cert       — Certified teacher (OH licensed)", 11, False, None),
        ("    Class      — Classified staff (custodial, cafeteria, aide, etc.)", 11, False, None),
        ("    Adm        — Administrator (principal, superintendent, etc.)", 11, False, None),
        ("    Adm-Class  — Administrator classified under union contract", 11, False, None),
        ("    xmpt       — Exempt / contracted (coaches, substitutes, etc.)", 11, False, None),
        ("", 11, False, None),
        ("Questions? See docs/DISTRICT_SETUP.pdf § HR Google Sheets.", 11, False, "808080"),
    ]
    for i, (txt, size, bold, color) in enumerate(lines, start=1):
        c = ws.cell(row=i, column=1, value=txt)
        kwargs = {"size": size, "bold": bold}
        if color:
            kwargs["color"] = color
        c.font = Font(**kwargs)


def main():
    wb = Workbook()
    _build_staff_tab(wb)
    _build_phone_tab(wb)
    _build_readme_tab(wb)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT_PATH)
    print(f"Wrote {OUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
