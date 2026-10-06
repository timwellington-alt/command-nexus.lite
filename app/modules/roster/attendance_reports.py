"""Attendance daily-digest email — content builder + send helpers.

Independent of the routed-alert system. Consumes the Phase C
analytics tables (student_analytics + attendance_daily_stats) and
emails one digest per building per day to the recipients configured
in attendance_report_recipients.

Empty building_code on a recipient means "district-wide" — that row
gets a rolled-up email covering every school.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import date, timedelta
from email.message import EmailMessage

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


DEFAULT_SENDER = "nexus.svc@yourdistrict.org"


# ── Data model helpers ───────────────────────────────────────────────


async def get_recipients(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT id, building_code, email, active, notes, created_at, created_by
        FROM attendance_report_recipients
        ORDER BY building_code, email
    """))).mappings().all()
    return [dict(r) for r in rows]


async def get_active_recipients_grouped(db: AsyncSession) -> dict[str, list[str]]:
    """Return {building_code: [email, ...]} for active recipients only."""
    rows = (await db.execute(text("""
        SELECT building_code, email
        FROM attendance_report_recipients
        WHERE active = true
        ORDER BY building_code, email
    """))).all()
    grouped: dict[str, list[str]] = {}
    for building, email in rows:
        grouped.setdefault(building, []).append(email)
    return grouped


async def add_recipient(
    db: AsyncSession, *, building_code: str, email: str,
    notes: str | None, actor: str,
) -> int:
    r = await db.execute(text("""
        INSERT INTO attendance_report_recipients
            (building_code, email, notes, created_by)
        VALUES (:b, :e, :n, :a)
        ON CONFLICT (building_code, email) DO UPDATE SET
            active = true,
            notes = COALESCE(EXCLUDED.notes, attendance_report_recipients.notes)
        RETURNING id
    """).bindparams(b=building_code or "", e=email.strip().lower(),
                    n=notes, a=actor))
    return r.scalar_one()


async def set_active(db: AsyncSession, recipient_id: int, active: bool) -> bool:
    r = await db.execute(text("""
        UPDATE attendance_report_recipients SET active = :a WHERE id = :i
    """).bindparams(a=active, i=recipient_id))
    return (r.rowcount or 0) > 0


async def delete_recipient(db: AsyncSession, recipient_id: int) -> bool:
    r = await db.execute(text(
        "DELETE FROM attendance_report_recipients WHERE id = :i"
    ).bindparams(i=recipient_id))
    return (r.rowcount or 0) > 0


# ── Digest content ──────────────────────────────────────────────────


async def build_building_digest(
    db: AsyncSession, *, building_code: str, report_date: date,
) -> dict | None:
    """Fetch today's actual absentee list for a building.

    Returns None when there's no data to report — caller skips send.

    ``student_absences.school_code`` stores raw SIS codes (SIS_A/SIS_B/
    SIS_C) because the ingest doesn't canonicalize. Config surfaces
    (recipients table, dropdowns) all use internal codes (PES/PHS/
    EPE). We translate internal → set-of-SIS at query time via
    school_building_map so a PES recipient sees the SIS_A absences.
    Empty building_code = all schools (district-wide).
    """
    from app.modules.settings.buildings import get_building_maps
    from app.modules.settings.repository import get_setting_value
    import json as _json

    bmap = await get_building_maps(db)
    sis_to_internal = bmap.get("sis_to_internal", {})
    # Reverse for name display: internal → full name (school_names is
    # keyed on SIS, so we walk through sis_to_internal).
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        names_map = _json.loads(names_raw)
    except Exception:
        names_map = {}
    internal_to_full: dict[str, str] = {}
    for sis, internal in sis_to_internal.items():
        full = names_map.get(sis)
        if full:
            internal_to_full[internal] = full

    is_district = not building_code
    display_name = (
        "District" if is_district
        else internal_to_full.get(building_code, building_code)
    )

    # Internal → list of matching SIS codes for the WHERE clause
    matching_sis = [
        sis for sis, internal in sis_to_internal.items() if internal == building_code
    ]
    if not is_district and not matching_sis:
        # Fall back to treating the passed code as SIS itself (defensive
        # against a recipient row stored with a raw SIS code)
        matching_sis = [building_code]

    params: dict = {"d": report_date}
    where_bc = ""
    if not is_district:
        where_bc = "AND school_code = ANY(:sis)"
        params["sis"] = matching_sis

    rows = (await db.execute(text(f"""
        SELECT sis_id, school_code, first_name, last_name, grade, homeroom,
               absence_type, absence_type_name, absence_level, absence_reason,
               absence_note, time_in, time_out, comments,
               primary_contact_name, primary_contact_phone
        FROM student_absences
        WHERE calendar_date = :d {where_bc}
        ORDER BY school_code, last_name, first_name
    """).bindparams(**params))).mappings().all()

    if not rows:
        return None

    # Translate each row's raw SIS school_code to the internal
    # abbreviation before handing to the renderer — never surface
    # SIS_A/SIS_B/SIS_C to a reader. See feedback_building_display_names.
    absentees = []
    for r in rows:
        d = dict(r)
        d["school_code"] = sis_to_internal.get(d.get("school_code"), d.get("school_code"))
        absentees.append(d)

    return {
        "display_name": display_name,
        "building_code": building_code,
        "report_date": report_date,
        "count": len(absentees),
        "absentees": absentees,
    }


def _esc(s: str | None) -> str:
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_digest_html(digest: dict, dashboard_url: str) -> str:
    show_school_col = not digest["building_code"]  # district view lists mixed schools

    header_cells = [
        "<th style='padding:6px 10px;text-align:left'>Name</th>",
        "<th style='padding:6px 10px;text-align:left'>Grade</th>",
        "<th style='padding:6px 10px;text-align:left'>Homeroom</th>",
        "<th style='padding:6px 10px;text-align:left'>Type</th>",
        "<th style='padding:6px 10px;text-align:left'>Reason</th>",
        "<th style='padding:6px 10px;text-align:left'>Time In</th>",
        "<th style='padding:6px 10px;text-align:left'>Time Out</th>",
        "<th style='padding:6px 10px;text-align:left'>Note</th>",
    ]
    if show_school_col:
        header_cells.insert(0, "<th style='padding:6px 10px;text-align:left'>School</th>")

    body_rows = []
    for a in digest["absentees"]:
        # Field-minimized name display — first initial only, matches
        # T0.4 staff-notification convention ("C. Adkins"). Kitchen
        # staff need enough to identify a student in context, not the
        # full PII record. Guidance-facing variants can override.
        first = (a.get("first_name") or "").strip()
        last = (a.get("last_name") or "").strip()
        initial = f"{first[0]}." if first else ""
        name = f"{last}, {initial}".rstrip(", ") if last else initial
        type_display = a.get("absence_type_name") or a.get("absence_type") or ""
        note = a.get("absence_note") or a.get("comments") or ""
        cells = [
            f"<td style='padding:6px 10px'>{_esc(name)}</td>",
            f"<td style='padding:6px 10px'>{_esc(a.get('grade'))}</td>",
            f"<td style='padding:6px 10px'>{_esc(a.get('homeroom'))}</td>",
            f"<td style='padding:6px 10px'>{_esc(type_display)}</td>",
            f"<td style='padding:6px 10px'>{_esc(a.get('absence_reason'))}</td>",
            f"<td style='padding:6px 10px'>{_esc(a.get('time_in'))}</td>",
            f"<td style='padding:6px 10px'>{_esc(a.get('time_out'))}</td>",
            f"<td style='padding:6px 10px;color:#666'>{_esc(note)}</td>",
        ]
        if show_school_col:
            cells.insert(0, f"<td style='padding:6px 10px'>{_esc(a.get('school_code'))}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return f"""\
<html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:14px;color:#222;max-width:960px">
  <h2 style="margin:0 0 4px;font-size:18px">{_esc(digest['display_name'])} &mdash; Absentees</h2>
  <div style="color:#666;font-size:12px;margin-bottom:16px">
    {digest['report_date'].isoformat()} &middot; {digest['count']} student{'' if digest['count']==1 else 's'} absent
  </div>

  <table style="border-collapse:collapse;font-size:13px;width:100%;border:1px solid #e5e5e5">
    <thead><tr style="background:#f7f7f7">{''.join(header_cells)}</tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table>
</body></html>
"""


# ── Send ────────────────────────────────────────────────────────────


def _gmail_client(sender_email: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    cred_file = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "/run/secrets/google_service_account.json",
    )
    creds = (
        service_account.Credentials.from_service_account_file(
            cred_file, scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        .with_subject(sender_email)
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _send_gmail_sync(sender: str, to: str, subject: str, body_html: str) -> str:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("This email requires an HTML-capable client.")
    msg.add_alternative(body_html, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = _gmail_client(sender)
    resp = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return resp.get("id", "")


async def send_digest_email(
    sender: str, to: str, subject: str, body_html: str,
) -> str:
    return await asyncio.to_thread(_send_gmail_sync, sender, to, subject, body_html)
