"""
Staff reconciliation service.

Cross-references AD vs Google vs Paxton to find mismatches.
All building/group mappings come from provisioning_profiles — nothing hardcoded.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.staff.models import StaffIgnore, StaffLink

logger = logging.getLogger(__name__)


async def run_reconciliation(db: AsyncSession) -> dict:
    """
    Cross-reference AD, Google Workspace, and Paxton users.
    Returns {staff: [...], summary: {...}}.

    All group/OU expectations come from provisioning_profiles or settings —
    no district-specific data hardcoded.
    """
    from app.integrations.ad.adapter import ActiveDirectoryAdapter
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.integrations.paxton.adapter import PaxtonAdapter

    ad = ActiveDirectoryAdapter(db)
    google = GoogleWorkspaceAdapter(db)
    paxton = PaxtonAdapter(db)

    # 1. Load AD staff
    try:
        ad_staff = await ad.list_staff_dicts(include_disabled=False)
        ad_by_email = {s["email"].lower(): s for s in ad_staff if s.get("email")}
        ad_by_username = {s["username"].lower(): s for s in ad_staff if s.get("username")}
    except Exception as e:
        logger.error(f"Reconciliation: AD fetch failed: {e}")
        ad_staff = []
        ad_by_email = {}
        ad_by_username = {}

    # 2. Load Google accounts
    try:
        google_accounts = await google.list_users(max_results=2000)
        google_by_email = {a["email"].lower(): a for a in google_accounts}
    except Exception as e:
        logger.error(f"Reconciliation: Google fetch failed: {e}")
        google_by_email = {}

    # 3. Load Paxton users
    try:
        paxton_users = await paxton.get_users()
        paxton_by_email = {u["email"].lower(): u for u in paxton_users if u.get("email")}
        paxton_by_name = {}
        paxton_by_lf = {}
        for u in paxton_users:
            dn = " ".join((u.get("display_name") or "").strip().lower().split())
            if dn:
                paxton_by_name[dn] = u
            fn = (u.get("first_name") or "").strip().lower()
            ln = (u.get("last_name") or "").strip().lower()
            if fn and ln:
                paxton_by_lf[f"{fn} {ln}"] = u
    except Exception as e:
        logger.error(f"Reconciliation: Paxton fetch failed: {e}")
        paxton_users = []
        paxton_by_email = {}
        paxton_by_name = {}
        paxton_by_lf = {}

    # 4. Load confirmed links
    link_result = await db.execute(select(StaffLink).where(StaffLink.confirmed == True))
    confirmed_links = {l.ad_username.lower(): l.paxton_id for l in link_result.scalars().all() if l.ad_username}

    # 5. Load ignored usernames
    ignored_result = await db.execute(
        select(StaffIgnore.username).where(StaffIgnore.restored_at == None)  # noqa: E711
    )
    ignored_usernames = {r[0].lower() for r in ignored_result.all()}

    # 6. Load HR data (optional)
    hr_emails: set[str] = set()
    hr_names: set[str] = set()
    try:
        from app.modules.settings.repository import get_setting_value
        sheet_id = await get_setting_value(db, "google", "hr_sheet_id")
        if sheet_id:
            from app.integrations.google.sheets_adapter import GoogleSheetsAdapter
            sheets = GoogleSheetsAdapter(db)
            hr_staff = await sheets.read_hr_staff(sheet_id)
            for h in hr_staff:
                if h.get("email"):
                    hr_emails.add(h["email"].lower())
                name_key = f"{h.get('first_name', '')} {h.get('last_name', '')}".lower().strip()
                if name_key:
                    hr_names.add(name_key)
    except Exception as e:
        logger.warning(f"Reconciliation: HR sheet failed: {e}")

    # 7. Build report — AD is the primary source
    summary = {
        "total_ad": len(ad_staff),
        "google_ok": 0, "google_missing": 0,
        "paxton_ok": 0, "paxton_missing": 0,
        "hr_ok": 0, "hr_missing": 0,
        "ignored": len(ignored_usernames),
    }
    report = []

    for s in ad_staff:
        username = (s.get("username") or "").lower()
        email = (s.get("email") or "").lower()
        fn = (s.get("first_name") or "").strip().lower()
        ln = (s.get("last_name") or "").strip().lower()
        display = s.get("display_name") or ""

        if username in ignored_usernames:
            continue

        entry = {
            "username": s.get("username"),
            "display_name": display,
            "email": s.get("email"),
            "building": s.get("building"),
            "role_type": s.get("role_type"),
            "title": s.get("title"),
            "enabled": s.get("enabled", False),
            "google_ok": False,
            "paxton_ok": False,
            "hr_active": None,
            "paxton_id": None,
            "issues": [],
        }

        # Google check
        if email and email in google_by_email:
            entry["google_ok"] = True
            summary["google_ok"] += 1
        else:
            entry["issues"].append("no_google")
            summary["google_missing"] += 1

        # Paxton check — confirmed link first, then email, then name
        pax_match = None
        if username in confirmed_links:
            pid = confirmed_links[username]
            pax_match = next((u for u in paxton_users if u["id"] == pid), None)
        if not pax_match and email:
            pax_match = paxton_by_email.get(email)
        if not pax_match:
            clean_name = " ".join(display.lower().split())
            pax_match = paxton_by_name.get(clean_name)
        if not pax_match and fn and ln:
            pax_match = paxton_by_lf.get(f"{fn} {ln}")

        if pax_match:
            entry["paxton_ok"] = True
            entry["paxton_id"] = pax_match["id"]
            summary["paxton_ok"] += 1
        else:
            entry["issues"].append("no_paxton")
            summary["paxton_missing"] += 1

        # HR check
        if hr_emails or hr_names:
            hr_found = (email in hr_emails) or (f"{fn} {ln}" in hr_names)
            entry["hr_active"] = hr_found
            if hr_found:
                summary["hr_ok"] += 1
            else:
                summary["hr_missing"] += 1

        report.append(entry)

    report.sort(key=lambda r: (r.get("display_name") or "").lower())

    return {
        "staff": report,
        "summary": summary,
    }
