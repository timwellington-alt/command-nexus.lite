"""
Student data audit helpers.

Provides explicit audit functions for high-sensitivity student data operations
that go beyond normal permission-based auditing.
"""

import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action

logger = logging.getLogger(__name__)


async def audit_student_export(
    db: AsyncSession,
    *,
    actor: str,
    export_type: str,
    record_count: int,
    scope: str | None = None,
    ip_address: str | None = None,
):
    """Audit a student data export. Called by any export endpoint."""
    return await log_action(
        db,
        actor=actor,
        action=f"student_data_export:{export_type}",
        module="roster",
        target=f"{record_count} records",
        outcome="success",
        details=f"type={export_type}, count={record_count}, scope={scope or 'all'}",
        ip_address=ip_address,
    )


async def audit_student_view(
    db: AsyncSession,
    *,
    actor: str,
    view_type: str,
    student_id: str | None = None,
    scope: str | None = None,
    ip_address: str | None = None,
):
    """Audit a sensitive student data view (profile, class list)."""
    return await log_action(
        db,
        actor=actor,
        action=f"student_data_view:{view_type}",
        module="roster",
        target=student_id,
        outcome="success",
        details=f"view={view_type}, scope={scope or 'all'}",
        ip_address=ip_address,
    )


async def audit_classlist_access(
    db: AsyncSession,
    *,
    actor: str,
    building: str,
    ip_address: str | None = None,
):
    """Audit class list access — required by FERPA tracking."""
    return await log_action(
        db,
        actor=actor,
        action="student_data_view:classlist",
        module="roster",
        target=building,
        outcome="success",
        details=f"building={building}",
        ip_address=ip_address,
    )
