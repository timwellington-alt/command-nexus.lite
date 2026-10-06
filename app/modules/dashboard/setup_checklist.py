"""First-run setup checklist for a fresh deployment.

Derives a list of "setup still needed" items from the current state
of the DB. Each item has:
  - key:      stable id (so the UI can dismiss individually)
  - label:    short imperative ("Add your buildings")
  - detail:   why it matters
  - link:     deep-link into the right Settings panel
  - complete: True iff the check passes

The dashboard renders every item with complete=False at the top of
the page. When they all pass, the card disappears.

Add new items by extending `_CHECKS` with another (label, detail,
link, async-check) tuple. Checks must be read-only + fast.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def _setting_present(db: AsyncSession, integration: str, key: str,
                           reject_defaults: tuple[str, ...] = ()) -> bool:
    """True iff a settings row exists with a non-empty value that
    isn't in the reject_defaults list (e.g. 'Your District')."""
    row = (await db.execute(text("""
        SELECT value FROM integration_configs
        WHERE integration = :i AND key = :k
    """).bindparams(i=integration, k=key))).mappings().first()
    if not row:
        return False
    val = (row["value"] or "").strip()
    if not val:
        return False
    return val not in reject_defaults


async def _check_district_name(db: AsyncSession) -> bool:
    return await _setting_present(
        db, "branding", "district_name",
        reject_defaults=("Your District", "the district", "", "Nexus"),
    )


async def _check_buildings(db: AsyncSession) -> bool:
    """At least one building configured. The settings UI writes the
    building map to branding.school_building_map (JSON)."""
    import json as _json
    row = (await db.execute(text("""
        SELECT value FROM integration_configs
        WHERE integration = 'branding' AND key = 'school_building_map'
    """))).mappings().first()
    if not row:
        return False
    try:
        parsed = _json.loads(row["value"] or "{}")
        return isinstance(parsed, dict) and len(parsed) > 0
    except Exception:
        return False


async def _check_default_admin_replaced(db: AsyncSession) -> bool:
    """True iff the default admin@local account has either been
    deleted OR had must_change_password cleared (meaning the operator
    actually logged in and set a real password). When this returns
    False, the deployment is still running with the known default
    credentials."""
    try:
        row = (await db.execute(text("""
            SELECT must_change_password FROM local_users
            WHERE lower(email) = 'admin@local'
        """))).mappings().first()
    except Exception:
        # local_users table may not exist (migration not run); treat as
        # "no default admin issue" so the checklist doesn't false-trip.
        return True
    if not row:
        return True
    return not row["must_change_password"]


async def _check_oauth_configured(db: AsyncSession) -> bool:
    """Only relevant when Google SSO is enabled. Returns True when
    the OAuth client ID secret file is present AND non-stub, OR when
    GOOGLE_AUTH_ENABLED is off (nothing to check)."""
    from app.config import get_settings
    s = get_settings()
    if not s.google_auth_enabled:
        return True  # not applicable
    cid = (s.google_client_id or "").strip()
    return bool(cid) and not cid.startswith("stub")


async def _check_sis_poller(db: AsyncSession) -> bool:
    """Clever/MetaSolutions email poller needs the mailbox to watch."""
    return await _setting_present(db, "google", "clever_gmail_user")


# (key, label, detail, link, check_fn)
_CHECKS: list[tuple[str, str, str, str, callable]] = [
    ("district_name",
     "Set your district name",
     "Shows up in the top nav, page titles, and outbound email branding.",
     "/settings#panel-branding",
     _check_district_name),
    ("buildings",
     "Add your buildings",
     "Nexus uses building codes everywhere — rosters, staff profiles, "
     "attendance reports, OU routing. Add at least one.",
     "/settings#panel-building_map",
     _check_buildings),
    ("default_admin",
     "Replace the default admin password",
     "The bootstrap admin@local account is still using the default "
     "password. Log out and back in to be forced through the password "
     "change flow, or delete the account from Local Accounts.",
     "/settings#panel-local_accounts",
     _check_default_admin_replaced),
    ("oauth_configured",
     "Finish Google SSO setup",
     "GOOGLE_AUTH_ENABLED is on but the OAuth client ID isn't "
     "configured yet — users will see 'Sign in with Google' but the "
     "button will fail. Follow docs/DISTRICT_SETUP.pdf.",
     "/settings#panel-google",
     _check_oauth_configured),
    ("sis_poller",
     "Configure your SIS mailbox",
     "Nexus polls a Gmail inbox for the SIS CSV exports (roster, "
     "attendance). Set the mailbox address in Settings → Google.",
     "/settings#panel-google",
     _check_sis_poller),
]


async def build_checklist(db: AsyncSession) -> list[dict]:
    """Run every check, return the list of items with their current
    completion state. The dashboard filters to incomplete-only."""
    out = []
    for key, label, detail, link, fn in _CHECKS:
        try:
            complete = bool(await fn(db))
        except Exception as e:
            logger.warning("setup_checklist[%s] failed: %s", key, e)
            complete = False
        out.append({
            "key": key,
            "label": label,
            "detail": detail,
            "link": link,
            "complete": complete,
        })
    return out
