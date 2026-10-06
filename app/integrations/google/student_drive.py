"""Per-student Google Drive adapter.

Used by the student_scan tool to enumerate + read + delete docs from
individual student Google accounts via domain-wide delegation. The
service account in /run/secrets/google_service_account.json must have
the scope set granted in Workspace Admin → Security → API Controls.

Required OAuth scopes for the SA's domain-wide delegation:
    https://www.googleapis.com/auth/drive
    https://www.googleapis.com/auth/documents.readonly
    https://www.googleapis.com/auth/spreadsheets.readonly

drive scope (NOT drive.readonly) is required for trash/delete.

This adapter is admin-tooling only — it touches student data. Every
public method that crosses the FERPA boundary is logged at the call
site via app.audit.service.log_action.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
]

# MIME types we know how to read text from. PDFs / images stay out of
# scope — we'd need OCR for those.
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"


@dataclass
class DriveDoc:
    """Subset of Drive metadata we care about. `owners` is normalized
    to a single email (Drive usually has one); `shared_with_me` flags
    docs the student can see but doesn't own (the proxy-list-circulating
    case)."""
    file_id: str
    name: str
    mime_type: str
    owner_email: str | None
    shared_with_me: bool
    modified_time: str | None  # RFC 3339


def _service_account_path() -> str:
    """Service account credentials live at the standard secret path
    used by every other Google integration in this codebase."""
    p = Path("/run/secrets/google_service_account.json")
    if not p.exists():
        # Dev fallback — same fallback the other adapters use
        p = Path("/app/secrets/google_service_account.json")
    if not p.exists():
        raise RuntimeError(
            "Google service account file not found at "
            "/run/secrets/google_service_account.json"
        )
    return str(p)


def _build_drive_client(student_email: str):
    """Build a Drive client impersonating one student via DWD."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = (
        service_account.Credentials
        .from_service_account_file(_service_account_path(), scopes=DRIVE_SCOPES)
        .with_subject(student_email)
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _build_docs_client(student_email: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = (
        service_account.Credentials
        .from_service_account_file(_service_account_path(), scopes=DRIVE_SCOPES)
        .with_subject(student_email)
    )
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _build_sheets_client(student_email: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = (
        service_account.Credentials
        .from_service_account_file(_service_account_path(), scopes=DRIVE_SCOPES)
        .with_subject(student_email)
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _build_slides_client(student_email: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = (
        service_account.Credentials
        .from_service_account_file(_service_account_path(), scopes=DRIVE_SCOPES)
        .with_subject(student_email)
    )
    return build("slides", "v1", credentials=creds, cache_discovery=False)


# ── List + filter ─────────────────────────────────────────────────────

async def list_docs_for_student(
    student_email: str,
    *,
    only_modified_since: str | None = None,
    page_size: int = 200,
    max_pages: int = 50,
) -> list[DriveDoc]:
    """List Docs + Sheets the student owns OR has shared-edit access to.

    `only_modified_since` is an RFC 3339 timestamp; if provided, only
    docs modified after that point are returned (for differential
    re-scans). `max_pages` caps the total to keep one student from
    eating the entire API budget on a hostile/runaway scan."""
    def _list():
        svc = _build_drive_client(student_email)
        # `q` filters MIME + mod-time. We restrict to Docs + Sheets +
        # Slides — slides matter because students embed hyperlinks
        # there to slip past keyword filters. PDFs/images need OCR
        # and stay out of scope.
        q_parts = [
            "(mimeType='application/vnd.google-apps.document' "
            "or mimeType='application/vnd.google-apps.spreadsheet' "
            "or mimeType='application/vnd.google-apps.presentation')",
            "trashed=false",
        ]
        if only_modified_since:
            q_parts.append(f"modifiedTime > '{only_modified_since}'")
        q = " and ".join(q_parts)

        results: list[DriveDoc] = []
        page_token = None
        for _ in range(max_pages):
            req = svc.files().list(
                q=q,
                pageSize=page_size,
                pageToken=page_token,
                fields=(
                    "nextPageToken,"
                    "files(id,name,mimeType,owners(emailAddress),"
                    "modifiedTime,sharedWithMeTime)"
                ),
                # Include shared-with-me docs (corpora=user is default).
                corpora="user",
            )
            resp = req.execute()
            for f in resp.get("files", []):
                owners = f.get("owners") or []
                owner_email = owners[0]["emailAddress"] if owners else None
                # `sharedWithMeTime` is set iff the doc was shared
                # with this user (vs owned by them).
                shared = bool(f.get("sharedWithMeTime"))
                results.append(DriveDoc(
                    file_id=f["id"],
                    name=f.get("name", ""),
                    mime_type=f["mimeType"],
                    owner_email=owner_email,
                    shared_with_me=shared,
                    modified_time=f.get("modifiedTime"),
                ))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results

    return await asyncio.to_thread(_list)


# ── Read text content ─────────────────────────────────────────────────

async def read_doc_text(student_email: str, file_id: str) -> str:
    """Pull the plain-text body of a Google Doc as the student. Returns
    empty string on auth/parse failure (logged) so the scan loop can
    keep going."""
    def _read():
        svc = _build_docs_client(student_email)
        doc = svc.documents().get(documentId=file_id).execute()
        # documents.get returns a structured tree of paragraphs +
        # runs of text. Flatten to plain text.
        out_parts: list[str] = []
        for elem in (doc.get("body") or {}).get("content") or []:
            para = elem.get("paragraph")
            if not para:
                continue
            for el in para.get("elements") or []:
                tr = el.get("textRun")
                if tr and tr.get("content"):
                    out_parts.append(tr["content"])
        return "".join(out_parts)
    try:
        return await asyncio.to_thread(_read)
    except Exception as e:
        logger.warning(
            "read_doc_text(%s, %s) failed: %s", student_email, file_id, e
        )
        return ""


async def read_sheet_text(student_email: str, file_id: str) -> str:
    """Pull all cell text from every tab of a Sheet, concatenated.

    Big sheets get capped at MAX_CELLS to avoid pulling megabytes per
    student. A proxy/game-list sheet is typically <1000 rows; if it's
    bigger it's almost certainly not what we're hunting for."""
    MAX_CELLS = 50_000

    def _read():
        svc = _build_sheets_client(student_email)
        meta = svc.spreadsheets().get(spreadsheetId=file_id, fields="sheets(properties(title))").execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets") or []]
        if not titles:
            return ""
        # Fetch up to all sheets in one batchGet
        ranges = titles[:20]  # cap tab count
        body = svc.spreadsheets().values().batchGet(
            spreadsheetId=file_id, ranges=ranges,
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        parts: list[str] = []
        cell_count = 0
        for vr in body.get("valueRanges") or []:
            for row in vr.get("values") or []:
                for cell in row:
                    if cell is None:
                        continue
                    parts.append(str(cell))
                    cell_count += 1
                    if cell_count >= MAX_CELLS:
                        return " ".join(parts)
        return " ".join(parts)
    try:
        return await asyncio.to_thread(_read)
    except Exception as e:
        logger.warning(
            "read_sheet_text(%s, %s) failed: %s", student_email, file_id, e
        )
        return ""


async def read_slides_text(student_email: str, file_id: str) -> str:
    """Pull text + hyperlink URLs from every slide in a Slides deck.

    Critical: slides are the favored bypass — students paste a normal
    word as the visible text and bury the proxy URL in the hyperlink
    target. We extract BOTH the visible text and `textRun.style.link.url`
    so the URL matcher sees the real destination."""
    def _read():
        svc = _build_slides_client(student_email)
        deck = svc.presentations().get(presentationId=file_id).execute()
        out_parts: list[str] = []

        def walk_text(text_block):
            for el in (text_block or {}).get("textElements") or []:
                tr = el.get("textRun")
                if not tr:
                    continue
                content = tr.get("content")
                if content:
                    out_parts.append(content)
                # Hyperlink target — the actual URL students try to hide
                link = (tr.get("style") or {}).get("link") or {}
                href = link.get("url")
                if href:
                    out_parts.append(" " + href + " ")

        def walk_page_elements(elements):
            for pe in elements or []:
                # Plain shapes — most slide text lives here
                shape = pe.get("shape") or {}
                walk_text(shape.get("text"))
                # Tables — each cell has its own text block
                table = pe.get("table") or {}
                for row in table.get("tableRows") or []:
                    for cell in row.get("tableCells") or []:
                        walk_text(cell.get("text"))
                # Groups — nested elements
                grp = pe.get("elementGroup") or {}
                walk_page_elements(grp.get("children"))

        for slide in deck.get("slides") or []:
            walk_page_elements(slide.get("pageElements"))
            # Speaker notes are also fair game (proxy URLs sometimes
            # hide there since they're invisible during presentation)
            notes = slide.get("slideProperties", {}).get("notesPage", {})
            walk_page_elements(notes.get("pageElements"))

        return "".join(out_parts)
    try:
        return await asyncio.to_thread(_read)
    except Exception as e:
        logger.warning(
            "read_slides_text(%s, %s) failed: %s", student_email, file_id, e
        )
        return ""


# ── Destructive actions ───────────────────────────────────────────────

async def trash_doc(student_email: str, file_id: str) -> None:
    """Move a doc to the student's trash (recoverable for 30 days).

    Caller must already have decided that:
      - The doc is genuinely a proxy/game list (review confirmed)
      - The owner is the student themselves OR another in-domain student

    Caller is also responsible for the audit log entry — this function
    does the API call only.
    """
    def _trash():
        svc = _build_drive_client(student_email)
        svc.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
    await asyncio.to_thread(_trash)


async def revoke_share_for_student(
    owner_email: str, file_id: str, student_email: str,
) -> None:
    """Strip `student_email` from a doc's permissions list. Used when
    the doc is owned by another student (or staff) — we can't trash
    that owner's doc, but we can take the suspect student's access
    away.

    Acts AS the owner via DWD so we don't need explicit owner consent.
    """
    def _revoke():
        svc = _build_drive_client(owner_email)
        # List permissions, find the one that matches student_email
        perms = svc.permissions().list(
            fileId=file_id,
            fields="permissions(id,emailAddress,role,type)",
            supportsAllDrives=True,
        ).execute()
        for p in perms.get("permissions") or []:
            if (p.get("emailAddress") or "").lower() == student_email.lower():
                svc.permissions().delete(
                    fileId=file_id, permissionId=p["id"],
                    supportsAllDrives=True,
                ).execute()
                return
    await asyncio.to_thread(_revoke)
