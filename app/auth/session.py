"""
Session utilities — get current user from session, resolve permissions.
"""

import json
from fastapi import HTTPException, Request, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import get_user_permissions


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """
    FastAPI dependency — resolves the authenticated user from the session.
    Raises 401 if not authenticated or user not found/inactive.
    """
    user_email = request.session.get("user_email")
    if not user_email:
        raise HTTPException(status_code=401, detail="Not authenticated")

    result = await db.execute(select(User).where(User.email == user_email))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    return user


async def get_current_user_permissions(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> tuple[User, list[dict]]:
    """
    FastAPI dependency — returns (user, permissions) tuple.
    Permissions include scope info for policy checks.
    """
    user = await get_current_user(request, db)
    permissions = await get_user_permissions(db, user.id)
    return user, permissions
