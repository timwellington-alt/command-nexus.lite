"""
Google Group → Role sync.

On each login, checks the user's Google Group memberships and
assigns the corresponding roles in the user_roles table.
Roles are re-evaluated on every login — group changes take effect
at next sign-in.
"""

import logging
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, Role, UserRole

logger = logging.getLogger(__name__)

async def _load_group_role_map(db: AsyncSession) -> dict:
    """
    Load Google Group → Role mappings from Settings.
    Returns {group_email: (role_names_list, scope_type, scope_value)}.
    Supports both old format {"role": "x"} and new {"roles": ["x","y"]}.
    """
    import json
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "role_sync", "group_role_map")
    if not raw:
        logger.warning("No group_role_map configured in Settings → Google Group → Role Mapping")
        return {}
    try:
        data = json.loads(raw)
        result = {}
        for group_email, v in data.items():
            roles = v.get("roles") or ([v["role"]] if v.get("role") else [])
            result[group_email] = (roles, v.get("scope", "district"), v.get("scope_value", "*"))
        return result
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error(f"Invalid group_role_map JSON: {e}")
        return {}


async def _check_group_membership(email: str, group_email: str) -> bool:
    """Check if an email is a member of a Google Group via Admin SDK.

    Runs in a thread pool to avoid blocking the async event loop.
    """
    import asyncio
    return await asyncio.to_thread(_check_group_membership_sync, email, group_email)


def _check_group_membership_sync(email: str, group_email: str) -> bool:
    """Synchronous group membership check — called via to_thread."""
    try:
        from app.config import get_settings
        import os

        settings = get_settings()
        cred_file = settings.google_service_account_file
        admin_email = settings.google_admin_email

        if not os.path.exists(cred_file) or not admin_email:
            logger.warning("Google service account not configured — skipping group check")
            return False

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = (
            service_account.Credentials
            .from_service_account_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/admin.directory.group.member.readonly"],
            )
            .with_subject(admin_email)
        )
        service = build("admin", "directory_v1", credentials=creds, cache_discovery=False)

        result = service.members().hasMember(
            groupKey=group_email,
            memberKey=email,
        ).execute()
        return result.get("isMember", False)

    except Exception as e:
        if "not a member" in str(e).lower() or "404" in str(e):
            return False
        logger.error(f"Group membership check failed for {email} in {group_email}: {e}")
        return False


async def sync_user_roles(db: AsyncSession, user: User) -> list[dict]:
    """
    Check Google Group memberships and sync roles in user_roles.

    Returns the list of assigned roles for the user.
    """
    email = user.email

    # Load all roles
    result = await db.execute(select(Role))
    role_lookup = {r.name: r for r in result.scalars().all()}

    # Load group → role map from Settings
    group_role_map = await _load_group_role_map(db)
    if not group_role_map:
        logger.warning(f"No group-role mappings configured — {email} gets no roles")
        return []

    # Load building canonicalization maps so any school-scoped entries
    # that still hold SIS codes get translated to internal codes at
    # assignment time. Belt + suspenders: the stored map was already
    # rewritten to internal codes during the a031-era migration, but
    # we don't want a future admin who pastes an SIS code into the
    # Settings UI to silently break scope comparisons.
    from app.modules.settings.buildings import get_building_maps, resolve_building_code_sync
    building_maps = await get_building_maps(db)

    # Check each group — each group can map to multiple roles
    assigned_roles = []
    for group_email, (role_names, scope_type, scope_value) in group_role_map.items():
        # Canonicalize school-scoped values to internal codes.
        canon_scope_value = scope_value
        if scope_type == "school" and scope_value and scope_value != "*":
            resolved = resolve_building_code_sync(scope_value, building_maps)
            if resolved:
                canon_scope_value = resolved

        if await _check_group_membership(email, group_email):
            for role_name in role_names:
                role = role_lookup.get(role_name)
                if not role:
                    logger.warning(f"Role '{role_name}' not found in DB — skipping {group_email}")
                    continue
                assigned_roles.append({
                    "role_id": role.id,
                    "role_name": role_name,
                    "scope_type": scope_type,
                    "scope_value": canon_scope_value,
                })

    if not assigned_roles:
        logger.warning(f"No group memberships found for {email}")
        return []

    # Clear existing roles and re-assign
    await db.execute(delete(UserRole).where(UserRole.user_id == user.id))

    for ar in assigned_roles:
        db.add(UserRole(
            user_id=user.id,
            role_id=ar["role_id"],
            scope_type=ar["scope_type"],
            scope_value=ar["scope_value"],
            assigned_by="google_group_sync",
        ))

    await db.flush()  # Stage role changes — caller (oauth callback) owns commit
    logger.info(f"Synced {len(assigned_roles)} roles for {email}: {[r['role_name'] for r in assigned_roles]}")

    return assigned_roles


def _check_2fa_enrolled_sync(email: str) -> bool:
    """
    Check whether a Google Workspace user has 2-Step Verification enrolled.

    Uses the Admin SDK users.get endpoint — requires the service account to
    have the admin.directory.user.readonly scope and domain-wide delegation.

    Fails CLOSED: any error (SDK unavailable, network issue, missing creds)
    returns False so the login is blocked rather than silently bypassed.
    This is intentional — a security check that can silently pass on error
    provides no security guarantee.
    """
    try:
        from app.config import get_settings
        import os

        settings = get_settings()
        cred_file = settings.google_service_account_file
        admin_email = settings.google_admin_email

        if not os.path.exists(cred_file) or not admin_email:
            logger.error(
                "2FA check: Google service account not configured — blocking login. "
                "Set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_ADMIN_EMAIL."
            )
            return False

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = (
            service_account.Credentials
            .from_service_account_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/admin.directory.user.readonly"],
            )
            .with_subject(admin_email)
        )
        service = build("admin", "directory_v1", credentials=creds, cache_discovery=False)

        user_data = service.users().get(
            userKey=email,
            fields="isEnrolledIn2Sv",
        ).execute()

        enrolled = user_data.get("isEnrolledIn2Sv", False)
        if not enrolled:
            logger.warning(f"2FA check: {email} does not have 2SV enrolled")
        return bool(enrolled)

    except Exception as e:
        logger.error(f"2FA check failed for {email}: {e} — blocking login (fail closed)")
        return False


async def check_2fa_enrolled(email: str) -> bool:
    """
    Async wrapper for _check_2fa_enrolled_sync.
    Runs the synchronous Admin SDK call in a thread pool.
    """
    import asyncio
    return await asyncio.to_thread(_check_2fa_enrolled_sync, email)
