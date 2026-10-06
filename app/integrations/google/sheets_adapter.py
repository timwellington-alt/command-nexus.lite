"""
Google Sheets adapter — reads HR staff data.

Uses the same service account as the Workspace adapter.
Column mappings are configurable via Settings.
"""

import logging
import os

logger = logging.getLogger(__name__)


# Row-level highlight colors for the room-roster pretty view. Applied
# by apply_room_layout_format() to data rows whose cell_meta carries a
# ``highlight`` marker set by build_sections().
_HIGHLIGHT_COLORS = {
    # Soft yellow for teachers on Leave of Absence.
    "loa": {"red": 1.0, "green": 0.9, "blue": 0.6},
    # Soft green for long-term subs filling in.
    "sub": {"red": 0.78, "green": 0.9, "blue": 0.78},
}


class GoogleSheetsAdapter:
    def __init__(self, db):
        self.db = db

    async def _get_service(self, write: bool = False):
        """
        Build a Sheets service client.

        Reads impersonate the workspace admin via domain-wide delegation
        so we can pull any HR/roster sheet in the domain.

        Writes use the service account's OWN identity (no subject
        impersonation) with the full spreadsheets scope. Target sheets
        must have the service account directly shared as an editor —
        this sidesteps needing the ``spreadsheets`` scope in
        Workspace Admin's domain-wide delegation.
        """
        from app.modules.settings.repository import get_setting_value

        cred_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/run/secrets/google_service_account.json")
        if not os.path.exists(cred_file):
            raise RuntimeError("Google service account not configured")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        if write:
            creds = service_account.Credentials.from_service_account_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
        else:
            admin_email = await get_setting_value(self.db, "google", "admin_email")
            if not admin_email:
                raise RuntimeError("Google admin_email not configured")
            creds = (
                service_account.Credentials
                .from_service_account_file(
                    cred_file,
                    scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
                )
                .with_subject(admin_email)
            )
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    async def read_sheet(self, sheet_id: str, tab_range: str = "A1:Z1000") -> list[list[str]]:
        """Read raw rows from a Google Sheet tab. Returns list of rows (list of cell values)."""
        import asyncio

        service = await self._get_service()

        def _read():
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=tab_range,
            ).execute()
            return result.get("values", [])

        return await asyncio.to_thread(_read)

    async def write_sheet(
        self,
        sheet_id: str,
        tab_range: str,
        values: list[list],
        clear_first: bool = True,
    ) -> dict:
        """
        Write a 2D array of values to a Google Sheet range.

        By default clears the target range before writing so stale
        content past the new payload doesn't linger (important for
        auto-generated layouts that shrink over time). Set
        ``clear_first=False`` to overlay onto existing content.
        """
        import asyncio

        service = await self._get_service(write=True)

        def _write():
            if clear_first:
                service.spreadsheets().values().clear(
                    spreadsheetId=sheet_id,
                    range=tab_range,
                    body={},
                ).execute()
            return service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=tab_range,
                valueInputOption="RAW",
                body={"values": values},
            ).execute()

        return await asyncio.to_thread(_write)

    async def append_row(
        self,
        sheet_id: str,
        tab_range: str,
        row: list,
    ) -> dict:
        """
        Append a single row to the end of a Google Sheets range. Uses
        ``values().append()`` with ``insertDataOption=INSERT_ROWS`` so
        Google finds the first blank row within the target range and
        drops the payload there — race-free vs. our own "find blank row
        first" logic.

        Used by the self-service onboarding form to add a new hire to
        the building's NexusData tab without clobbering existing rows.
        """
        import asyncio

        service = await self._get_service(write=True)

        def _append():
            return service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=tab_range,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()

        return await asyncio.to_thread(_append)

    async def ensure_tab(self, sheet_id: str, tab_title: str) -> None:
        """
        Add ``tab_title`` to the spreadsheet if it doesn't already
        exist. No-op if the tab is already present.
        """
        import asyncio

        service = await self._get_service(write=True)

        def _ensure():
            meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
            titles = {sh["properties"]["title"] for sh in meta.get("sheets", [])}
            if tab_title in titles:
                return
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab_title}}}]},
            ).execute()

        await asyncio.to_thread(_ensure)

    async def unmerge_tab(self, sheet_id: str, tab_title: str) -> None:
        """
        Drop every merged range on ``tab_title``. Needed before writing
        values to a tab cloned from a formatted source — writes into
        non-master cells of a merge are silently dropped, so cloned
        merges must be cleared or the payload comes out with holes.
        """
        import asyncio

        service = await self._get_service(write=True)

        def _unmerge():
            meta = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets(properties,merges)",
            ).execute()
            target = next(
                (sh for sh in meta.get("sheets", []) if sh["properties"]["title"] == tab_title),
                None,
            )
            if not target:
                return
            sheet_id_num = target["properties"]["sheetId"]
            grid = target["properties"].get("gridProperties", {})
            end_row = grid.get("rowCount", 1000)
            end_col = grid.get("columnCount", 26)
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [{
                        "unmergeCells": {
                            "range": {
                                "sheetId": sheet_id_num,
                                "startRowIndex": 0, "endRowIndex": end_row,
                                "startColumnIndex": 0, "endColumnIndex": end_col,
                            },
                        },
                    }],
                },
            ).execute()

        await asyncio.to_thread(_unmerge)

    async def apply_room_layout_format(
        self,
        sheet_id: str,
        tab_title: str,
        cell_meta: list[dict],
        template_tab: str = "Room #s",
        header_sample: tuple[int, int] = (0, 0),
        data_sample_center: tuple[int, int] = (2, 0),
        data_sample_right: tuple[int, int] = (2, 4),
        data_sample_name: tuple[int, int] = (2, 1),
    ) -> None:
        """
        Re-apply the Room #s layout's formatting rules to ``tab_title``,
        with **fonts, sizes, colors, and borders sampled at runtime from
        the template tab**. That way whenever the template's typography
        changes (e.g. someone bumps header size or changes color),
        the auto tab tracks — no code change needed.

        Rules:
          - Column widths (12 cols; the template's actual widths).
          - Section-header merges per block (A:C, E:G, I:L).
          - Spacer-row merges (same but I:K for block 3).
          - Header cells copy the exact userEnteredFormat of the sample
            template header cell.
          - Data cells copy from three template samples: room-center,
            room-right (block 2), and name (leftmost name cell) — so
            per-block alignment differences carry over.

        Idempotent — safe to call after every write. ``cell_meta`` is
        the list produced by ``render_layout`` describing which grid
        rows are header/spacer/data per block.
        """
        import asyncio

        service = await self._get_service(write=True)

        def _apply():
            # Look up numeric sheet ID.
            meta = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets(properties(sheetId,title))",
            ).execute()
            sheet_id_num = None
            for sh in meta.get("sheets", []):
                if sh["properties"]["title"] == tab_title:
                    sheet_id_num = sh["properties"]["sheetId"]
                    break
            if sheet_id_num is None:
                raise RuntimeError(f"Tab {tab_title!r} not found")

            # ── Sample formatting from the template ────────────────
            def _cell_to_a1(row: int, col: int) -> str:
                col_letter = ""
                n = col + 1
                while n:
                    n, r = divmod(n - 1, 26)
                    col_letter = chr(65 + r) + col_letter
                return f"{col_letter}{row + 1}"

            sample_ranges = [
                f"'{template_tab}'!{_cell_to_a1(*header_sample)}",
                f"'{template_tab}'!{_cell_to_a1(*data_sample_center)}",
                f"'{template_tab}'!{_cell_to_a1(*data_sample_right)}",
                f"'{template_tab}'!{_cell_to_a1(*data_sample_name)}",
            ]
            sample = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                ranges=sample_ranges,
                includeGridData=True,
                # effectiveFormat resolves theme defaults into concrete
                # values — matters for cells where halign / valign was
                # inherited from the sheet rather than set explicitly
                # (e.g. block-2 room cells inheriting RIGHT).
                fields="sheets(data(rowData(values(effectiveFormat))))",
            ).execute()

            # Strip fields we don't want to blanket-write. `padding` and
            # `wrapStrategy` come along in effectiveFormat but shouldn't
            # override the destination's defaults; borders we set
            # separately per row/block so the sample's borders are
            # noise.
            _STRIP_KEYS = ("padding", "wrapStrategy", "hyperlinkDisplayType", "borders")

            def _extract_fmt(idx: int) -> dict:
                # All four sample_ranges hit the same template tab so
                # batchGet returns them as separate `data` entries under
                # sheets[0], not as separate `sheets`. Index the data
                # array, not the sheets array.
                try:
                    sheet_data = sample["sheets"][0]["data"][idx]
                    values = sheet_data["rowData"][0]["values"]
                    fmt = dict(values[0].get("effectiveFormat", {}) or {})
                except (KeyError, IndexError):
                    return {}
                for k in _STRIP_KEYS:
                    fmt.pop(k, None)
                return fmt

            # Borders sampled from the template lose their color-style
            # metadata in transit and confuse the write. Apply borders
            # from the rules (grid style for data, full box for header)
            # after sampling. Font/color/alignment still come from the
            # template.
            black_border = {
                "style": "SOLID",
                "color": {"red": 0.0, "green": 0.0, "blue": 0.0},
            }
            header_borders = {
                "top": black_border, "bottom": black_border,
                "left": black_border, "right": black_border,
            }
            data_borders = {"bottom": black_border, "right": black_border}

            header_fmt = {**_extract_fmt(0), "borders": header_borders}
            data_center_fmt = {**_extract_fmt(1), "borders": data_borders}
            data_right_fmt = {**_extract_fmt(2), "borders": data_borders}
            data_name_fmt = {**_extract_fmt(3), "borders": data_borders}

            # Column widths from the template. D (31) and H (38) are
            # deliberate visual dividers between blocks; keeping them
            # narrow is what makes the 3-column layout read like columns
            # instead of one wide table.
            col_widths = {
                0: 64, 1: 92, 2: 77, 3: 31,   # A-D
                4: 49, 5: 93, 6: 77, 7: 38,   # E-H
                8: 49, 9: 86, 10: 107, 11: 123,   # I-L
            }

            # Block coordinate math:
            #   block 1 = cols A-D  (0..3)
            #   block 2 = cols E-H  (4..7)
            #   block 3 = cols I-L  (8..11)
            # Header merges span 3 cols in blocks 1&2, 4 in block 3.
            # Spacer merges span 3 cols in all three (block 3 leaves L
            # unmerged so its width matches the sub-role column below).
            block_start = {1: 0, 2: 4, 3: 8}
            header_span = {1: 3, 2: 3, 3: 4}
            spacer_span = {1: 3, 2: 3, 3: 3}

            # `fields` mask for repeatCell — enumerate every property
            # we might set from the template so subsequent runs can't
            # accidentally leave stale format on cells that changed.
            FULL_FIELDS = (
                "userEnteredFormat("
                "backgroundColor,backgroundColorStyle,textFormat,"
                "horizontalAlignment,verticalAlignment,borders,"
                "padding,wrapStrategy)"
            )

            requests: list = []

            # ── 0. Whole-tab background reset ───────────────────────
            # Any cell not touched by a subsequent rule below (header,
            # data, spacer) inherits its background from the initial
            # template clone — so a section-header gray at Sheet1!A22
            # bleeds into Directory (Auto)!A22 even after the row's
            # content is replaced by an empty spacer or falls beyond
            # the rendered range. Wipe everything to white first, then
            # let header_fmt paint gray back onto header cells only.
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id_num,
                        "startRowIndex": 0, "endRowIndex": 500,
                        "startColumnIndex": 0, "endColumnIndex": 12,
                    },
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                    }},
                    "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle",
                },
            })

            # ── 1. Column widths ───────────────────────────────────
            for idx, px in col_widths.items():
                requests.append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id_num,
                            "dimension": "COLUMNS",
                            "startIndex": idx,
                            "endIndex": idx + 1,
                        },
                        "properties": {"pixelSize": px},
                        "fields": "pixelSize",
                    },
                })

            # ── 2. Per-row merges + formatting from cell_meta ──────
            def _range(r: int, c_start: int, c_end: int) -> dict:
                return {
                    "sheetId": sheet_id_num,
                    "startRowIndex": r, "endRowIndex": r + 1,
                    "startColumnIndex": c_start, "endColumnIndex": c_end,
                }

            for entry in cell_meta:
                row = entry["row"]
                block = entry["block"]
                kind = entry["kind"]
                start_col = block_start[block]

                if kind == "header":
                    span = header_span[block]
                    rng = _range(row, start_col, start_col + span)
                    requests.append({"mergeCells": {"range": rng, "mergeType": "MERGE_ALL"}})
                    if header_fmt:
                        requests.append({
                            "repeatCell": {
                                "range": rng,
                                "cell": {"userEnteredFormat": header_fmt},
                                "fields": FULL_FIELDS,
                            },
                        })
                elif kind == "spacer":
                    span = spacer_span[block]
                    rng = _range(row, start_col, start_col + span)
                    requests.append({"mergeCells": {"range": rng, "mergeType": "MERGE_ALL"}})
                    # Spacer rows sit between sections and should be
                    # visually blank. Explicitly clear background/borders
                    # inherited from the initial template clone — otherwise
                    # a formatted cell that used to hold a section header
                    # keeps its gray fill even after the row's content is
                    # wiped by write_sheet.
                    requests.append({
                        "repeatCell": {
                            "range": rng,
                            "cell": {"userEnteredFormat": {
                                "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                                "borders": {},
                            }},
                            "fields": "userEnteredFormat(backgroundColor,backgroundColorStyle,borders)",
                        },
                    })
                elif kind == "data":
                    # Room cell: block 2 uses the right-aligned template
                    # sample (since the source has room numbers right-
                    # aligned in the middle block for visual balance),
                    # blocks 1 & 3 use the center-aligned sample.
                    room_fmt = data_right_fmt if block == 2 else data_center_fmt
                    if room_fmt:
                        requests.append({
                            "repeatCell": {
                                "range": _range(row, start_col, start_col + 1),
                                "cell": {"userEnteredFormat": room_fmt},
                                "fields": FULL_FIELDS,
                            },
                        })
                    # Last + first + subrole cells: sampled from the
                    # name template cell (left-aligned).
                    if data_name_fmt:
                        requests.append({
                            "repeatCell": {
                                "range": _range(row, start_col + 1, start_col + 4),
                                "cell": {"userEnteredFormat": data_name_fmt},
                                "fields": FULL_FIELDS,
                            },
                        })
                    # Highlight override for LOA / Teacher-Sub rows
                    # merged into their grade section. Painted AFTER
                    # the base data format so it overrides the sampled
                    # white background.
                    highlight = entry.get("highlight") or ""
                    if highlight in _HIGHLIGHT_COLORS:
                        requests.append({
                            "repeatCell": {
                                "range": _range(row, start_col, start_col + 4),
                                "cell": {"userEnteredFormat": {
                                    "backgroundColor": _HIGHLIGHT_COLORS[highlight],
                                }},
                                "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle",
                            },
                        })

            # Sheets API's batchUpdate accepts thousands of requests per
            # call, but the payload has a hard cap. Split into chunks
            # of 250 to stay well under it and to keep any single
            # failure narrowly scoped.
            CHUNK = 250
            for i in range(0, len(requests), CHUNK):
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": requests[i:i + CHUNK]},
                ).execute()

        await asyncio.to_thread(_apply)

    async def clone_tab(
        self,
        sheet_id: str,
        source_title: str,
        dest_title: str,
        replace_if_exists: bool = True,
    ) -> None:
        """
        Duplicate ``source_title`` as ``dest_title``. Preserves ALL
        formatting: column widths, borders, cell styles, merged
        cells, and print settings (including "fit to page").

        Delete-and-recreate if ``replace_if_exists`` is True — the
        Sheets API's duplicateSheet can't overwrite an existing tab,
        so we drop the old copy first to get a clean formatting
        inheritance every run.

        The destination is always forced visible even if the source is
        hidden — layout templates are typically hidden from admin view,
        but the rendered output tab is what people actually look at.
        """
        import asyncio

        service = await self._get_service(write=True)

        def _clone():
            meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
            src_id = None
            dest_id = None
            for sh in meta.get("sheets", []):
                p = sh["properties"]
                if p["title"] == source_title:
                    src_id = p["sheetId"]
                if p["title"] == dest_title:
                    dest_id = p["sheetId"]
            if src_id is None:
                raise RuntimeError(f"Source tab {source_title!r} not found")

            requests = []
            if dest_id is not None:
                if not replace_if_exists:
                    return  # nothing to do
                requests.append({"deleteSheet": {"sheetId": dest_id}})
            requests.append({
                "duplicateSheet": {
                    "sourceSheetId": src_id,
                    "newSheetName": dest_title,
                },
            })
            resp = service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": requests},
            ).execute()
            # duplicateSheet returns the new sheet's properties (last
            # reply corresponds to the duplicate request). Force-visible
            # even if the source was hidden.
            new_id = None
            for reply in resp.get("replies", []):
                props = reply.get("duplicateSheet", {}).get("properties")
                if props:
                    new_id = props.get("sheetId")
            if new_id is not None:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{
                        "updateSheetProperties": {
                            "properties": {"sheetId": new_id, "hidden": False},
                            "fields": "hidden",
                        },
                    }]},
                ).execute()

        await asyncio.to_thread(_clone)

    async def read_hr_staff(self, sheet_id: str) -> list[dict]:
        """
        Read the HR master staff list. Column positions (configurable via Settings):
        C=Last, D=First, F=School, K=Classification, M=Position, T=Email

        TODO: Make column mappings configurable via Settings.
        """
        rows = await self.read_sheet(sheet_id, "MASTER!A1:V500")
        staff = []

        for r in rows[1:]:  # Skip header
            if len(r) < 4:
                continue
            last = (r[2] if len(r) > 2 else "").strip()
            first = (r[3] if len(r) > 3 else "").strip()
            if not last or not first:
                continue

            salutation = (r[4] if len(r) > 4 else "").strip()
            email = (r[19] if len(r) > 19 else "").strip().lower()
            staff.append({
                "first_name": first,
                "last_name": last,
                "salutation": salutation,
                "name": f"{first} {last}".strip(),
                "email": email,
                "school": (r[5] if len(r) > 5 else "").strip(),
                "classification": (r[10] if len(r) > 10 else "").strip(),
                "position": (r[12] if len(r) > 12 else "").strip(),
            })

        # Aides tab
        try:
            aides_rows = await self.read_sheet(sheet_id, "Aides Only!A1:M100")
            for r in aides_rows[1:]:
                if len(r) < 3:
                    continue
                first = (r[1] if len(r) > 1 else "").strip()
                last = (r[2] if len(r) > 2 else "").strip()
                if not last or not first:
                    continue
                staff.append({
                    "first_name": first,
                    "last_name": last,
                    "email": "",
                    "school": (r[3] if len(r) > 3 else "").strip(),
                    "classification": "Aide",
                    "position": (r[12] if len(r) > 12 else "").strip(),
                })
        except Exception as e:
            logger.warning(f"Could not read Aides Only tab: {e}")

        return staff
