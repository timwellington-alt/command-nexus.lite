"""
Policy engine — centralized authorization.

All permission checks go through here. No route handler or service
should make authorization decisions outside this layer.

Evaluates both action permission and scope.
"""

import json
from fastapi import HTTPException, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import User, UserRole, Role, RolePermission, Permission


async def get_user_permissions(db: AsyncSession, user_id: int) -> list[dict]:
    """
    Load all permissions for a user, including scope info.
    Returns list of {action, scope_type, scope_value, is_student_sensitive}.

    Building-scoped perms get their ``scope_value`` normalized through
    ``branding.school_building_map`` so an SIS code (SIS_B) compares
    equal to its internal code (PHS) in downstream visibility checks
    (custodial dispatcher scope, principal building scope, etc.).
    Without this, a perm with scope_value='SIS_B' wouldn't match a
    ticket with building='PHS' and the dispatcher would see nothing.
    """
    result = await db.execute(
        select(
            Permission.action,
            Permission.is_student_sensitive,
            UserRole.scope_type,
            UserRole.scope_value,
        )
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    rows = list(result.all())

    # One settings lookup per call; settings has its own per-process
    # cache, so this is effectively free after warm-up.
    bldg_map = {}
    if any(r.scope_type in ("school", "building") and r.scope_value for r in rows):
        from app.modules.settings.repository import get_setting_value
        raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                bldg_map = parsed
        except json.JSONDecodeError:
            pass

    out = []
    for row in rows:
        scope_value = row.scope_value
        if scope_value and row.scope_type in ("school", "building"):
            scope_value = bldg_map.get(scope_value, scope_value)
        out.append({
            "action": row.action,
            "is_student_sensitive": row.is_student_sensitive,
            "scope_type": row.scope_type,
            "scope_value": scope_value,
        })
    return out


def check_permission(
    user_permissions: list[dict],
    required_action: str,
    scope_type: str | None = None,
    scope_value: str | None = None,
) -> bool:
    """
    Check if user has the required permission, optionally within a scope.

    Rules:
    - district scope with '*' grants access to everything
    - specific scope (e.g. school=PHS) grants access only within that scope
    - admin inherits the richest view in every context
    """
    for perm in user_permissions:
        if perm["action"] != required_action:
            continue

        # No scope required — action match is enough
        if scope_type is None:
            return True

        # District-wide access
        if perm["scope_type"] == "district" and perm["scope_value"] == "*":
            return True

        # Matching scope
        if perm["scope_type"] == scope_type and perm["scope_value"] == scope_value:
            return True

    return False


def require_action(action: str, scope_type: str | None = None, scope_value: str | None = None):
    """
    FastAPI dependency factory for permission checks.

    Usage:
        @router.get("/staff")
        async def list_staff(user=Depends(require_action("staff.view"))):

    With scope:
        @router.get("/roster/students")
        async def students(user=Depends(require_action("roster.students.view", "school", "PHS"))):

    Returns the User object if authorized, raises 403 if denied.
    """
    from app.auth.session import get_current_user

    async def checker(
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> "User":
        user = await get_current_user(request, db)
        permissions = await get_user_permissions(db, user.id)

        if not check_permission(permissions, action, scope_type, scope_value):
            from app.audit.service import log_action
            await log_action(
                db,
                actor=user.email,
                action=f"denied:{action}",
                module=action.split(".")[0],
                outcome="denied",
                details=f"scope={scope_type}:{scope_value}" if scope_type else None,
                ip_address=request.client.host if request.client else None,
            )
            await db.commit()  # Persist denial audit — no route handler will run
            raise HTTPException(status_code=403, detail=f"Permission denied: {action}")

        # Audit student-sensitive access (FERPA access log foundation)
        is_sensitive = any(
            p["action"] == action and p["is_student_sensitive"]
            for p in permissions
        )
        if is_sensitive:
            from app.audit.service import log_action
            await log_action(
                db,
                actor=user.email,
                action=f"student_data_access:{action}",
                module=action.split(".")[0],
                outcome="success",
                details=f"scope={scope_type}:{scope_value}" if scope_type else None,
                ip_address=request.client.host if request.client else None,
            )

        return user

    return checker


def require_any_action(*actions: str):
    """FastAPI dependency factory: allow if user has ANY of the given actions.

    Used when a single resource is accessible via more than one role —
    e.g. facility floor-plan GETs are valid for both inventory editors
    (full edit access) and security viewers (SRO-style read-only).

    Denies (403) only if NONE match. Logs the denial under the first
    listed action so audit trails are coherent.
    """
    from app.auth.session import get_current_user

    async def checker(
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> "User":
        user = await get_current_user(request, db)
        permissions = await get_user_permissions(db, user.id)
        for action in actions:
            if check_permission(permissions, action):
                return user
        from app.audit.service import log_action
        await log_action(
            db,
            actor=user.email,
            action=f"denied:{actions[0]}",
            module=actions[0].split(".")[0],
            outcome="denied",
            details=f"any_of={','.join(actions)}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        raise HTTPException(status_code=403, detail=f"Permission denied: any of {actions}")

    return checker
