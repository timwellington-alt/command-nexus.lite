"""
Notifications platform service.

Owned by the worker layer, used by Staff and Roster modules.
Sends via Gmail API using the service account.

IMPORTANT — T0.4 field minimization rules:
- Staff notifications: first initial + last name + SID + email (no full first name)
- Guidance notifications: full name + SID + grade + school (no email)
- Never include: contact info, DOB, addresses, phone numbers
- Notification content is not configurable by end users
"""

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings

logger = logging.getLogger(__name__)


# ── Field minimization helpers (T0.4) ─────────────────────────────────────

def _minimize_for_staff(first_name: str, last_name: str) -> str:
    """Staff notification format: first initial + last name (e.g. 'C. Adkins')."""
    initial = first_name[0].upper() if first_name else "?"
    return f"{initial}. {last_name}"


def _minimize_for_guidance(first_name: str, last_name: str) -> str:
    """Guidance notification format: full name (counselors need the name to schedule)."""
    return f"{first_name} {last_name}"


# ── Email sending ─────────────────────────────────────────────────────────

def _build_gmail_service(sender_email: str):
    """Build Gmail API service using domain-wide delegation."""
    settings = get_settings()
    cred_file = settings.google_service_account_file

    if not os.path.exists(cred_file):
        raise FileNotFoundError(f"Service account file not found: {cred_file}")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = (
        service_account.Credentials
        .from_service_account_file(
            cred_file,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        .with_subject(sender_email)
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _send_email(sender: str, to: str, subject: str, html: str) -> bool:
    """Low-level email send. Returns True on success."""
    if not to or "@" not in to:
        logger.warning(f"Notification skipped — invalid recipient: {to!r}")
        return False

    try:
        service = _build_gmail_service(sender)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        msg.attach(MIMEText(html, "html"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Notification sent → {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Notification failed → {to}: {e}")
        return False


def _html_wrap(
    body: str,
    district_name: str = "",
    app_url: str = "",
    link_label: str = "Open Nexus",
    link_path: str = "",
) -> str:
    """
    Wrap a notification body in the standard branded shell.

    When ``app_url`` is provided, a primary-button link is rendered at
    the bottom of the body so recipients have a one-click path back
    into the app. Set ``link_path`` to deep-link into a specific page
    (e.g. ``/roster`` or ``/staff``); it's appended to ``app_url``.
    ``link_label`` lets callers override the button text when a more
    specific call-to-action is warranted ("Open Guidance Queue",
    "Review Staff Queue", etc.).

    This is the shared wrapper for every outbound notification — adding
    the link here (instead of each template) keeps the link consistent
    across staff / guidance / transport / provisioning emails without
    forcing every call site to build the same button HTML.
    """
    footer = f"{district_name} Nexus" if district_name else "Nexus"

    link_html = ""
    if app_url:
        base = app_url.rstrip("/")
        path = link_path or ""
        if path and not path.startswith("/"):
            path = "/" + path
        href = f"{base}{path}"
        link_html = (
            f'<div style="margin:24px 0 8px;text-align:center">'
            f'<a href="{href}" '
            f'style="display:inline-block;padding:10px 22px;background:#58a6ff;'
            f'color:#fff;text-decoration:none;border-radius:6px;font-size:14px;'
            f'font-weight:600">{link_label}</a>'
            f'</div>'
        )

    return f"""
<div style="font-family:Arial,sans-serif;max-width:600px;color:#222;line-height:1.5">
  {body}
  {link_html}
  <hr style="border:none;border-top:1px solid #e0e0e0;margin:28px 0 16px">
  <p style="font-size:12px;color:#888;margin:0">
    {footer} &mdash; Automated message. Do not reply.
  </p>
</div>"""


def parse_contact(value: str) -> tuple[str, str]:
    """Parse 'Name <email>' format into (name, email). Falls back to (value, value)."""
    import re
    m = re.match(r"^(.+?)\s*<(.+?)>$", value.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Might just be a plain email
    if "@" in value:
        return value.split("@")[0], value.strip()
    return value.strip(), ""


# ── Typed notification methods ────────────────────────────────────────────

def notify_staff_credential(
    sender: str,
    recipient: str,
    *,
    first_name: str,
    last_name: str,
    building: str,
    title: str,
    google_email: str,
    start_date: str | None = None,
) -> bool:
    """
    Staff credential delivery — sends to building principal.

    Field minimization (T0.4): Uses first initial + last name only.
    Email is included (needed for account provisioning context).
    Never includes: DOB, contact info, addresses, phone numbers.
    """
    display_name = _minimize_for_staff(first_name, last_name)
    subject = f"New Staff Account: {display_name}"

    html = _html_wrap(f"""
    <p>A new staff account has been created:</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Name</td><td>{display_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Position</td><td>{title}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Building</td><td>{building}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Email</td><td style="font-family:monospace">{google_email}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Start Date</td><td>{start_date or 'TBD'}</td></tr>
    </table>
    <p>The new staff member will need to change their password on first login.</p>
    """)

    return _send_email(sender, recipient, subject, html)


def notify_guidance_queue(
    sender: str,
    recipient: str,
    *,
    counselor_name: str,
    students: list[dict],
    subject_template: str = "",
    body_template: str = "",
) -> bool:
    """
    Guidance queue alert — sends to counselor + configured recipients.

    Field minimization (T0.4): Full name + SID + grade + school.
    No email in guidance notifications (counselors don't need it).
    Never includes: DOB, contact info, addresses, phone numbers.

    Available template tags:
      {count}            — number of students
      {s}                — "s" if count > 1, empty if 1
      {s_have}           — "s have" if count > 1, " has" if 1
      {s_need}           — "s" if count > 1, empty if 1
      {counselor_name}   — full counselor name
      {counselor_first}  — first name of counselor
      {student_table}    — HTML table of students (Name, SID, Grade, School)
      {student_list}     — plain text list of student names

    students: list of {first_name, last_name, student_id, grade, school_code}
    """
    first = counselor_name.split()[0] if counselor_name else "Counselor"
    count = len(students)

    # Build student table
    rows = "".join(
        f"<tr>"
        f"<td style='padding:4px 12px 4px 0'>{_minimize_for_guidance(s['first_name'], s['last_name'])}</td>"
        f"<td style='padding:4px 12px;font-family:monospace;font-size:13px'>{s.get('student_id', '')}</td>"
        f"<td style='padding:4px 12px'>{s.get('grade', '')}</td>"
        f"<td style='padding:4px 12px'>{s.get('school_code', '')}</td>"
        f"</tr>"
        for s in students
    )
    student_table = (
        f"<table style='border-collapse:collapse;margin:16px 0;font-size:14px'>"
        f"<tr style='font-weight:bold;border-bottom:1px solid #ccc'>"
        f"<td style='padding:4px 12px 4px 0'>Student</td>"
        f"<td style='padding:4px 12px'>SID</td>"
        f"<td style='padding:4px 12px'>Grade</td>"
        f"<td style='padding:4px 12px'>School</td></tr>"
        f"{rows}</table>"
    )
    student_list = ", ".join(
        _minimize_for_guidance(s["first_name"], s["last_name"]) for s in students
    )

    # Template replacements
    tags = {
        "count": str(count),
        "s": "s" if count != 1 else "",
        "s_have": "s have" if count != 1 else " has",
        "s_need": "s" if count != 1 else "",
        "counselor_name": counselor_name,
        "counselor_first": first,
        "student_table": student_table,
        "student_list": student_list,
    }

    # Use templates or defaults
    if not subject_template:
        subject_template = "{count} New Student{s} Awaiting Schedule"
    if not body_template:
        body_template = (
            "<p>Hi {counselor_first},</p>"
            "<p>{count} new student{s_have} been registered and need{s_need} a schedule assigned:</p>"
            "{student_table}"
            "<p>Please log in to the Guidance Queue to view details and mark as scheduled once complete.</p>"
        )

    subject = subject_template
    body = body_template
    for tag, value in tags.items():
        subject = subject.replace("{" + tag + "}", value)
        body = body.replace("{" + tag + "}", value)

    html = _html_wrap(body)
    return _send_email(sender, recipient, subject, html)


def notify_queue_entry_created(
    sender: str,
    recipient: str,
    *,
    first_name: str,
    last_name: str,
    building: str,
    action: str,
    source: str,
) -> bool:
    """
    Notify principal/reception that a new queue entry was auto-created.

    Full names used (adult staff, not T0.4 minimized).
    """
    full_name = f"{first_name} {last_name}"
    action_label = "provisioning" if action == "provision" else "deprovisioning"
    source_label = {"hr_sync": "HR sync", "room_roster": "room roster", "manual": "manual entry"}.get(source, source)
    subject = f"Staff Queue: {full_name} — {action_label} pending"

    html = _html_wrap(f"""
    <p>A new staff {action_label} entry has been created from <strong>{source_label}</strong>:</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Name</td><td>{full_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Building</td><td>{building}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Action</td><td>{action_label.title()}</td></tr>
    </table>
    <p>Please review and complete any missing information in the Staff Queue.</p>
    """)

    return _send_email(sender, recipient, subject, html)


def notify_provisioned(
    sender: str,
    recipient: str,
    *,
    first_name: str,
    last_name: str,
    building: str,
    position: str,
    email: str,
    room: str = "",
    include_credentials: bool = False,
    temp_password: str = "",
) -> bool:
    """
    Notify that a staff member has been provisioned.

    Full names used (adult staff, not T0.4 minimized).
    Credentials only sent to reception recipients.
    """
    full_name = f"{first_name} {last_name}"
    subject = f"Staff Provisioned: {full_name}"

    cred_row = ""
    if include_credentials and temp_password:
        cred_row = f'<tr><td style="padding:4px 16px 4px 0;font-weight:bold">Temp Password</td><td style="font-family:monospace;background:#1a1a2e;padding:4px 8px;border-radius:4px">{temp_password}</td></tr>'

    html = _html_wrap(f"""
    <p>A new staff member has been provisioned at <strong>{building}</strong>:</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Name</td><td>{full_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Position</td><td>{position or '-'}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Building</td><td>{building}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Email</td><td style="font-family:monospace">{email}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Room</td><td>{room or '-'}</td></tr>
      {cred_row}
    </table>
    <p>The new staff member will need to change their password on first login.</p>
    """)

    return _send_email(sender, recipient, subject, html)


def notify_deprovisioned(
    sender: str,
    recipient: str,
    *,
    first_name: str,
    last_name: str,
    building: str,
    position: str = "",
) -> bool:
    """
    Notify that a staff member has been deprovisioned.

    Full names used (adult staff, not T0.4 minimized).
    """
    full_name = f"{first_name} {last_name}"
    subject = f"Staff Deprovisioned: {full_name}"

    html = _html_wrap(f"""
    <p>A staff member has been deprovisioned at <strong>{building}</strong>:</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Name</td><td>{full_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Position</td><td>{position or '-'}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Building</td><td>{building}</td></tr>
    </table>
    <p>All system access has been revoked.</p>
    """)

    return _send_email(sender, recipient, subject, html)


def notify_student_account(
    sender: str,
    recipient: str,
    *,
    first_name: str,
    last_name: str,
    student_id: str,
    grade: str,
    school: str,
    google_email: str | None = None,
    google_provisioned: bool = False,
) -> bool:
    """
    Student account notice — sends to homeroom teacher.

    Field minimization (T0.4): First initial + last name + SID + email.
    Never includes: full first name, DOB, contact info, addresses.
    """
    display_name = _minimize_for_staff(first_name, last_name)
    subject = f"New Student Enrolled: {display_name}"

    if google_provisioned and google_email:
        account_line = f"<span style='font-family:monospace'>{google_email}</span>"
        account_note = f"Their Google Workspace account is active at <strong style='font-family:monospace'>{google_email}</strong>."
    else:
        account_line = "<span style='color:#c0392b'>Pending &mdash; IT will follow up</span>"
        account_note = "A Google Workspace account could not be created automatically. IT will resolve this shortly."

    html = _html_wrap(f"""
    <p>A new student has been added to your roster:</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Student</td><td>{display_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">SID</td><td style="font-family:monospace">{student_id}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Grade</td><td>{grade}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">School</td><td>{school}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Google Account</td><td>{account_line}</td></tr>
    </table>
    <p>{account_note}</p>
    """)


def notify_ticket_on_behalf_of(
    sender: str,
    recipient: str,
    *,
    ticket_id: int,
    title: str,
    category: str,
    sub_category: str | None,
    priority: str,
    description: str | None,
    building: str | None,
    room: str | None,
    submitter_name: str,
    submitter_email: str,
    is_registered: bool,
    app_url: str,
    district_name: str,
) -> bool:
    """Email the person a ticket was filed for.

    Fired at ticket-create time whenever ``on_behalf_of_email`` is set.
    Two variants:
    - ``is_registered=True``: recipient already has a Nexus account;
      pitch the CTA as "view your ticket" and deep-link to the ticket.
    - ``is_registered=False``: recipient isn't in ``users`` yet; pitch
      the CTA as "sign in for the first time" so they land in the
      dashboard where the ticket will be waiting once first-login
      backfill links their email → user id (see auth/oauth.py).
    """
    sub_pretty = (sub_category or "").replace("_", " ")
    location = " / ".join(p for p in (building, room and f"Rm {room}") if p)
    prio_badge = ""
    if (priority or "").lower() in ("urgent", "high"):
        color = "#c62828" if priority == "urgent" else "#e67e22"
        prio_badge = (
            f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:10px;font-size:11px;text-transform:uppercase;'
            f'letter-spacing:0.05em;font-weight:600">{priority}</span>'
        )

    if is_registered:
        opener = (
            f"<p>{submitter_name} filed a ticket for you in "
            f"<strong>{district_name or 'Nexus'}</strong>. It's on your "
            f"account and you can track its progress at any time.</p>"
        )
        cta_label = "View ticket"
        cta_path = f"/tickets/{ticket_id}"
    else:
        opener = (
            f"<p>{submitter_name} filed a ticket for you in "
            f"<strong>{district_name or 'Nexus'}</strong>, our district's "
            f"support system. You don't have to do anything &mdash; the "
            f"ticket is being handled &mdash; but you can sign in with "
            f"your <span style='font-family:monospace'>@{submitter_email.split('@',1)[1] if '@' in submitter_email else 'district'}</span> "
            f"Google account any time to see updates, add details, or "
            f"file your own tickets going forward.</p>"
        )
        cta_label = "Sign in to Nexus"
        cta_path = "/tickets/submit"

    desc_block = ""
    if description and description.strip():
        # Keep it short — this is a nudge, not the full detail page.
        snip = description.strip()
        if len(snip) > 400:
            snip = snip[:400].rstrip() + "…"
        # Preserve line breaks with <br>, but keep raw text safe by
        # relying on the caller having sanitized upstream (this content
        # was submitted through our own form).
        import html as _html
        safe = _html.escape(snip).replace("\n", "<br>")
        desc_block = (
            f'<tr><td style="padding:4px 16px 4px 0;font-weight:bold;'
            f'vertical-align:top">Details</td><td>{safe}</td></tr>'
        )

    body = f"""
    {opener}
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Ticket</td>
          <td>#{ticket_id} &mdash; {title} {prio_badge}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Category</td>
          <td>{category.capitalize()}{f' &middot; {sub_pretty}' if sub_pretty else ''}</td></tr>
      {f'<tr><td style="padding:4px 16px 4px 0;font-weight:bold">Location</td><td>{location}</td></tr>' if location else ''}
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Filed by</td>
          <td>{submitter_name} &lt;<span style="font-family:monospace">{submitter_email}</span>&gt;</td></tr>
      {desc_block}
    </table>
    """

    subject = f"[Nexus] Ticket #{ticket_id} filed for you: {title}"
    return _send_email(sender, recipient, subject, _html_wrap(
        body,
        district_name=district_name,
        app_url=app_url,
        link_label=cta_label,
        link_path=cta_path,
    ))


def notify_repair_swap_complete(
    sender: str,
    recipient: str,
    *,
    ticket_id: int,
    on_behalf_of: str | None,
    old_serial: str,
    new_serial: str,
    new_model: str | None,
    asset_tag: str | None,
    cart_ou: str | None,
    tech_email: str,
    app_url: str,
    district_name: str,
) -> bool:
    """Email the drop-off contact after a Chromebook is physically
    replaced via the depot swap flow. Explains what changed in plain
    English so the recipient knows the "new" device carries their
    old device's asset tag and belongs in the same classroom OU."""
    tag_line = (f"<strong>{asset_tag}</strong>" if asset_tag
                else "<em>(no asset tag set)</em>")
    on_behalf_html = (f"<tr><td style='padding:4px 16px 4px 0;font-weight:bold'>For</td>"
                      f"<td>{on_behalf_of}</td></tr>") if on_behalf_of else ""
    location_html = (f"<tr><td style='padding:4px 16px 4px 0;font-weight:bold'>Cart OU</td>"
                     f"<td style='font-family:monospace;font-size:12px'>{cart_ou}</td></tr>"
                     if cart_ou else "")

    body = f"""
    <p>Heads up &mdash; the Chromebook you dropped off for repair
    (ticket #{ticket_id}) couldn't be repaired, so we replaced it with a
    spare unit. The <strong>asset tag stayed the same</strong> and the
    new device has been placed in the same classroom cart OU, so from
    a management standpoint it behaves like the same device.</p>

    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Ticket</td>
          <td>#{ticket_id}</td></tr>
      {on_behalf_html}
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Old serial</td>
          <td style="font-family:monospace">{old_serial}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">New serial</td>
          <td style="font-family:monospace">{new_serial}{f' &middot; {new_model}' if new_model else ''}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Asset tag</td>
          <td>{tag_line} <span style="color:#888;font-size:12px">(unchanged)</span></td></tr>
      {location_html}
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Swapped by</td>
          <td style="font-family:monospace">{tech_email}</td></tr>
    </table>

    <p>The replacement is ready for pickup. If anything looks off &mdash;
    wrong device, wrong tag, unexpected OU &mdash; reply to this email or
    reopen the ticket and we'll sort it out.</p>
    """

    subject = f"[Nexus] Ticket #{ticket_id}: Chromebook replaced with a spare"
    return _send_email(sender, recipient, subject, _html_wrap(
        body,
        district_name=district_name,
        app_url=app_url,
        link_label="View ticket",
        link_path=f"/tickets/{ticket_id}",
    ))


def notify_onboarding_submitted(
    sender: str,
    recipient: str,
    *,
    first_name: str,
    last_name: str,
    building: str,
    assignment: str,
    room: str | None,
    district_name: str,
) -> bool:
    """Confirmation email to the new hire, sent the moment they submit
    the self-service onboarding form. No credentials, no district
    login — just "we got your info, next step is on us"."""
    full_name = f"{first_name} {last_name}"
    location = " · ".join(p for p in (building, room and f"Room {room}") if p)
    body = f"""
    <p>Hi {first_name} — thanks for submitting your onboarding info. Your
    submission has been logged and the tech team will get to work
    setting up your district accounts.</p>

    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Name</td>
          <td>{full_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Building</td>
          <td>{location or building}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Role</td>
          <td>{assignment}</td></tr>
    </table>

    <p>You'll get one more email when your accounts are ready. That
    message will tell you where to go to pick up your login and a
    temporary building badge. No further action is needed from you
    right now.</p>

    <p style="color:#888;font-size:12px">If any of the info above looks
    wrong, or if you didn't submit this form, please reply to this
    email and we'll sort it out.</p>
    """
    subject = f"[{district_name or 'Nexus'}] We got your onboarding info — {full_name}"
    return _send_email(sender, recipient, subject, _html_wrap(
        body, district_name=district_name,
    ))


def notify_onboarding_provisioned(
    sender: str,
    recipient: str,
    *,
    first_name: str,
    last_name: str,
    google_email: str,
    building: str,
    front_office_location: str | None,
    district_name: str,
) -> bool:
    """Welcome email to the new hire once an admin actually provisions
    their Google account. Deliberately does NOT include the password —
    that lives on a physical credential label printed at the building's
    front office, along with a temporary building badge. Recipient
    picks both up in person."""
    full_name = f"{first_name} {last_name}"
    pickup_line = (
        f"the <strong>{front_office_location} front office</strong>"
        if front_office_location else "your building's front office"
    )
    body = f"""
    <p>Hi {first_name} — welcome aboard. Your district Google account is
    ready.</p>

    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Google email</td>
          <td style="font-family:monospace">{google_email}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Sign in at</td>
          <td><a href="https://accounts.google.com/">accounts.google.com</a></td></tr>
    </table>

    <p><strong>Stop by {pickup_line}</strong> to pick up your temporary
    password and building badge. Bring a photo ID. Your password will be
    printed on a label the front office prints for you — please change
    it as soon as you sign in for the first time.</p>

    <p>Any trouble with the sign-in? Reply to this email or ask the
    front office to loop in tech.</p>
    """
    subject = f"[{district_name or 'Nexus'}] Your {building} account is ready — {full_name}"
    return _send_email(sender, recipient, subject, _html_wrap(
        body, district_name=district_name,
    ))


# ── Chromebook depot lifecycle ────────────────────────────────────────────
# Fires at two moments: reception (device arrived at depot) and completion
# (either repaired-and-ready, or beyond-repair terminal). Recipient is
# resolved via resolve_notify_user() in the caller — on_behalf_of if set,
# otherwise the ticket requester.

def _device_line(serial: str, asset_tag: str | None, model: str | None) -> str:
    """Human-readable device identifier for depot emails."""
    parts = []
    if asset_tag: parts.append(f"tag {asset_tag}")
    if serial:    parts.append(f"S/N {serial}")
    if model:     parts.append(model)
    return " · ".join(parts) if parts else "your Chromebook"


def notify_repair_received(
    sender: str,
    recipient: str,
    *,
    ticket_id: int,
    ticket_title: str,
    serial: str,
    asset_tag: str | None,
    model: str | None,
    app_url: str,
    district_name: str,
) -> bool:
    """Fired when a repair ticket transitions to `received` — the depot
    has physically taken possession of the Chromebook."""
    device = _device_line(serial, asset_tag, model)
    body = f"""
    <p>The repair depot has received your Chromebook.</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Ticket</td>
          <td>#{ticket_id} &mdash; {ticket_title}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Device</td>
          <td>{device}</td></tr>
    </table>
    <p>You'll get another email once it's ready to pick up or if we
    determine it can't be repaired.</p>
    """
    subject = f"[{district_name or 'Nexus'}] Chromebook received — ticket #{ticket_id}"
    return _send_email(sender, recipient, subject, _html_wrap(
        body, district_name=district_name,
        app_url=app_url, link_label="View ticket", link_path=f"/tickets/{ticket_id}",
    ))


def notify_repair_completion(
    sender: str,
    recipient: str,
    *,
    ticket_id: int,
    ticket_title: str,
    disposition: str,          # 'ready_return' | 'beyond_repair'
    serial: str,
    asset_tag: str | None,
    model: str | None,
    diagnosed_issue: str | None,
    app_url: str,
    district_name: str,
) -> bool:
    """Fired at terminal completion — either the repair is done and the
    Chromebook is ready to pick up, or it's been declared beyond repair.

    For beyond-repair with a planned swap, a separate
    ``notify_repair_swap_complete`` will follow once the replacement is
    physically deployed. This message is the "heads up" that fires on
    the beyond_repair status transition.
    """
    device = _device_line(serial, asset_tag, model)
    if disposition == "ready_return":
        opener = (
            "<p>Your Chromebook has been repaired and is ready for pickup "
            "at the tech office.</p>"
        )
        subject = f"[{district_name or 'Nexus'}] Chromebook ready for pickup — ticket #{ticket_id}"
    else:
        opener = (
            "<p>Your Chromebook has been determined to be beyond repair. "
            "The tech team will follow up shortly with next steps (a "
            "replacement device or disposal + reissue).</p>"
        )
        subject = f"[{district_name or 'Nexus'}] Chromebook beyond repair — ticket #{ticket_id}"

    diagnosis_block = ""
    if diagnosed_issue:
        import html as _html
        diagnosis_block = (
            f'<tr><td style="padding:4px 16px 4px 0;font-weight:bold;vertical-align:top">'
            f'Findings</td><td>{_html.escape(diagnosed_issue)}</td></tr>'
        )

    body = f"""
    {opener}
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Ticket</td>
          <td>#{ticket_id} &mdash; {ticket_title}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Device</td>
          <td>{device}</td></tr>
      {diagnosis_block}
    </table>
    """
    return _send_email(sender, recipient, subject, _html_wrap(
        body, district_name=district_name,
        app_url=app_url, link_label="View ticket", link_path=f"/tickets/{ticket_id}",
    ))
