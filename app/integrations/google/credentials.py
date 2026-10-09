"""Shared Google service-account credential loader.

Two sources, in priority order:
  1. ``google.service_account_json`` setting (encrypted in
     ``integration_configs``) — set via Settings → Google Workspace
     by pasting the full JSON key. Preferred because it survives
     a container restart without the operator having to re-upload
     the file.
  2. ``/run/secrets/google_service_account.json`` — classic
     docker-secrets-mount path. Still supported for operators who
     prefer file-based credentials. Falls back to this when the DB
     setting is empty or unparseable.

Both the main Workspace adapter and the Sheets/Drive adapters go
through this module so operators have ONE place to configure the
key, and one place to rotate it when needed.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CRED_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "/run/secrets/google_service_account.json",
)


async def load_service_account_credentials(
    db,
    *,
    scopes: Iterable[str],
    subject: Optional[str] = None,
):
    """Return a google.oauth2.service_account.Credentials object, or
    None when no credentials are available. ``subject`` optionally
    enables domain-wide-delegation impersonation of a Workspace user
    (callers set this for admin-SDK scopes; omit for the sheet
    service-account-shared-as-editor path)."""
    from google.oauth2 import service_account
    from app.modules.settings.repository import get_setting_value

    scopes_list = list(scopes)

    # 1. DB-stored JSON
    sa_json_raw = await get_setting_value(db, "google", "service_account_json")
    if sa_json_raw:
        try:
            info = json.loads(sa_json_raw)
            if info.get("type") == "service_account" and info.get("private_key"):
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=scopes_list,
                )
                if subject:
                    creds = creds.with_subject(subject)
                return creds
            logger.warning("google.service_account_json present but missing type/private_key")
        except Exception as e:
            logger.warning(f"google.service_account_json unparseable: {e}")

    # 2. mounted file
    if os.path.isfile(_DEFAULT_CRED_FILE) and os.path.getsize(_DEFAULT_CRED_FILE) > 0:
        creds = service_account.Credentials.from_service_account_file(
            _DEFAULT_CRED_FILE, scopes=scopes_list,
        )
        if subject:
            creds = creds.with_subject(subject)
        return creds

    return None
