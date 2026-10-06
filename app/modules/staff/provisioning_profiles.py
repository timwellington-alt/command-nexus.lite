"""
Provisioning profiles — configurable group/OU/access mappings per building+role.

Every value comes from the database, set via the Settings UI.
No district-specific data is hardcoded. A new district configures their
own buildings, roles, OUs, groups, and access levels through the interface.

Profile resolution: building+role → {google_ou, google_groups[], ad_ou, ad_groups[], paxton_access_level}
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, String, Text, Integer, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import Base

logger = logging.getLogger(__name__)


class ProvisioningProfile(Base):
    """
    Maps a building+role combination to the groups/OUs/access levels
    that should be assigned when provisioning a new staff member.

    All values are district-configurable via the Settings UI.
    """
    __tablename__ = "provisioning_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    building: Mapped[str] = mapped_column(String(50), nullable=False)  # PHS, PES, EPE, CO, DIST, or * for default
    role_type: Mapped[str] = mapped_column(String(50), nullable=False)  # teacher, admin, classified, tech, sub, or * for default

    # Google Workspace
    google_ou: Mapped[str | None] = mapped_column(String(500))  # OU path e.g. /Users-Teachers/PES
    google_groups: Mapped[str | None] = mapped_column(Text)  # JSON array of group emails

    # Active Directory
    ad_ou: Mapped[str | None] = mapped_column(String(500))  # OU name e.g. PES-Teachers
    ad_groups: Mapped[str | None] = mapped_column(Text)  # JSON array of AD group names

    # Paxton Net2
    paxton_access_level: Mapped[str | None] = mapped_column(String(255))  # Access level name
    paxton_department_id: Mapped[int | None] = mapped_column(Integer)
    paxton_department_name: Mapped[str | None] = mapped_column(String(255))  # Display only

    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


async def get_profile(db: AsyncSession, building: str, role_type: str) -> dict:
    """
    Resolve the provisioning profile for a building+role combination.

    Resolution order:
    1. Exact match: building+role
    2. Building default: building+*
    3. Role default: *+role
    4. Global default: *+*
    5. Empty profile (no groups assigned)

    Returns dict with google_ou, google_groups, ad_ou, ad_groups, paxton_access_level.
    """
    candidates = [
        (building, role_type),
        (building, "*"),
        ("*", role_type),
        ("*", "*"),
    ]

    for b, r in candidates:
        result = await db.execute(
            select(ProvisioningProfile).where(
                ProvisioningProfile.building == b,
                ProvisioningProfile.role_type == r,
            )
        )
        profile = result.scalar_one_or_none()
        if profile:
            return {
                "building": profile.building,
                "role_type": profile.role_type,
                "google_ou": profile.google_ou,
                "google_groups": json.loads(profile.google_groups) if profile.google_groups else [],
                "ad_ou": profile.ad_ou,
                "ad_groups": json.loads(profile.ad_groups) if profile.ad_groups else [],
                "paxton_access_level": profile.paxton_access_level,
                "paxton_department_id": profile.paxton_department_id,
                "paxton_department_name": profile.paxton_department_name,
            }

    return {
        "building": building,
        "role_type": role_type,
        "google_ou": None,
        "google_groups": [],
        "ad_ou": None,
        "ad_groups": [],
        "paxton_access_level": None,
        "paxton_department_id": None,
        "paxton_department_name": None,
    }


async def list_profiles(db: AsyncSession) -> list[dict]:
    """List all provisioning profiles."""
    result = await db.execute(
        select(ProvisioningProfile).order_by(
            ProvisioningProfile.building, ProvisioningProfile.role_type
        )
    )
    return [
        {
            "id": p.id,
            "building": p.building,
            "role_type": p.role_type,
            "google_ou": p.google_ou,
            "google_groups": json.loads(p.google_groups) if p.google_groups else [],
            "ad_ou": p.ad_ou,
            "ad_groups": json.loads(p.ad_groups) if p.ad_groups else [],
            "paxton_access_level": p.paxton_access_level,
            "paxton_department_id": p.paxton_department_id,
            "paxton_department_name": p.paxton_department_name,
            "updated_by": p.updated_by,
        }
        for p in result.scalars().all()
    ]


async def upsert_profile(
    db: AsyncSession,
    *,
    building: str,
    role_type: str,
    google_ou: str | None = None,
    google_groups: list[str] | None = None,
    ad_ou: str | None = None,
    ad_groups: list[str] | None = None,
    paxton_access_level: str | None = None,
    paxton_department_id: int | None = None,
    paxton_department_name: str | None = None,
    updated_by: str | None = None,
) -> ProvisioningProfile:
    """Create or update a provisioning profile. Caller must commit."""
    result = await db.execute(
        select(ProvisioningProfile).where(
            ProvisioningProfile.building == building,
            ProvisioningProfile.role_type == role_type,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.google_ou = google_ou
        existing.google_groups = json.dumps(google_groups or [])
        existing.ad_ou = ad_ou
        existing.ad_groups = json.dumps(ad_groups or [])
        existing.paxton_access_level = paxton_access_level
        existing.paxton_department_id = paxton_department_id
        existing.paxton_department_name = paxton_department_name
        existing.updated_by = updated_by
        existing.updated_at = datetime.now(timezone.utc)
        return existing

    profile = ProvisioningProfile(
        building=building,
        role_type=role_type,
        google_ou=google_ou,
        google_groups=json.dumps(google_groups or []),
        ad_ou=ad_ou,
        ad_groups=json.dumps(ad_groups or []),
        paxton_access_level=paxton_access_level,
        paxton_department_id=paxton_department_id,
        paxton_department_name=paxton_department_name,
        updated_by=updated_by,
    )
    db.add(profile)
    await db.flush()
    return profile


async def delete_profile(db: AsyncSession, profile_id: int) -> bool:
    """Delete a provisioning profile. Caller must commit."""
    result = await db.execute(
        select(ProvisioningProfile).where(ProvisioningProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if profile:
        await db.delete(profile)
        return True
    return False
