"""
Staff repository — DB access for staff requests and workflows.

No commit inside — routers own transactions.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.staff.models import (
    StaffRequest, StaffWorkflowRun, StaffWorkflowStep, StaffLink,
)

logger = logging.getLogger(__name__)


# ── Requests ──────────────────────────────────────────────────────────────

async def create_request(db: AsyncSession, **kwargs) -> StaffRequest:
    """Stage a new staff request. Caller must commit."""
    req = StaffRequest(**kwargs)
    db.add(req)
    await db.flush()  # Get ID without committing
    return req


async def get_request(db: AsyncSession, request_id: int) -> StaffRequest | None:
    result = await db.execute(select(StaffRequest).where(StaffRequest.id == request_id))
    return result.scalar_one_or_none()


async def list_requests(
    db: AsyncSession,
    request_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[StaffRequest]:
    q = select(StaffRequest).order_by(desc(StaffRequest.submitted_at)).limit(limit)
    if request_type:
        q = q.where(StaffRequest.request_type == request_type)
    if status:
        q = q.where(StaffRequest.status == status)
    result = await db.execute(q)
    return list(result.scalars().all())


# ── Workflow Runs ─────────────────────────────────────────────────────────

async def create_workflow_run(
    db: AsyncSession,
    request_id: int,
    run_type: str,
    started_by: str,
) -> StaffWorkflowRun:
    """Stage a new workflow run. Caller must commit."""
    run = StaffWorkflowRun(
        request_id=request_id,
        run_type=run_type,
        started_by=started_by,
    )
    db.add(run)
    await db.flush()
    return run


async def create_workflow_step(
    db: AsyncSession,
    run_id: int,
    step_name: str,
) -> StaffWorkflowStep:
    """Stage a new workflow step. Caller must commit."""
    step = StaffWorkflowStep(run_id=run_id, step_name=step_name)
    db.add(step)
    await db.flush()
    return step


async def update_step(
    db: AsyncSession,
    step: StaffWorkflowStep,
    *,
    status: str,
    before_state: dict | None = None,
    after_state: dict | None = None,
    result_data: dict | None = None,
    error_message: str | None = None,
) -> None:
    """Update step outcome. Caller must commit."""
    step.status = status
    step.completed_at = datetime.now(timezone.utc)
    if before_state:
        step.before_state = json.dumps(before_state)
    if after_state:
        step.after_state = json.dumps(after_state)
    if result_data:
        step.result_data = json.dumps(result_data)
    if error_message:
        step.error_message = error_message


async def get_workflow_runs(db: AsyncSession, request_id: int) -> list[StaffWorkflowRun]:
    result = await db.execute(
        select(StaffWorkflowRun)
        .where(StaffWorkflowRun.request_id == request_id)
        .order_by(desc(StaffWorkflowRun.started_at))
    )
    return list(result.scalars().all())


async def get_workflow_steps(db: AsyncSession, run_id: int) -> list[StaffWorkflowStep]:
    result = await db.execute(
        select(StaffWorkflowStep)
        .where(StaffWorkflowStep.run_id == run_id)
        .order_by(StaffWorkflowStep.id)
    )
    return list(result.scalars().all())


# ── Staff Links ───────────────────────────────────────────────────────────

async def get_link_by_username(db: AsyncSession, ad_username: str) -> StaffLink | None:
    result = await db.execute(
        select(StaffLink).where(StaffLink.ad_username == ad_username)
    )
    return result.scalar_one_or_none()


async def get_link_by_email(db: AsyncSession, google_email: str) -> StaffLink | None:
    """Fallback lookup by Google email when AD username is not available."""
    result = await db.execute(
        select(StaffLink).where(StaffLink.google_email == google_email)
    )
    return result.scalar_one_or_none()


async def get_link_by_paxton_id(db: AsyncSession, paxton_id: int) -> StaffLink | None:
    """Last-resort lookup by Paxton ID when neither AD username nor email is available."""
    result = await db.execute(
        select(StaffLink).where(StaffLink.paxton_id == paxton_id)
    )
    return result.scalar_one_or_none()


async def upsert_link(
    db: AsyncSession,
    *,
    ad_username: str | None = None,
    google_email: str | None = None,
    paxton_id: int | None = None,
    match_type: str = "auto",
) -> StaffLink:
    """
    Write or update a staff identity link.

    At least one recoverable identity anchor is required — ad_username or google_email.
    paxton_id alone is not sufficient: offboarding cannot look up a link from a Paxton ID
    without first resolving it through AD username or email. A paxton-ID-only row would
    be written but never found at offboard time.

    Lookup priority: ad_username → google_email. Merges any new fields into
    an existing record rather than creating a duplicate.
    """
    if not any([ad_username, google_email]):
        raise ValueError("upsert_link requires ad_username or google_email")

    # Find an existing record using the best available anchor
    existing = None
    if ad_username:
        existing = await get_link_by_username(db, ad_username)
    if existing is None and google_email:
        existing = await get_link_by_email(db, google_email)

    if existing:
        if ad_username:
            existing.ad_username = ad_username
        if google_email:
            existing.google_email = google_email
        if paxton_id:
            existing.paxton_id = paxton_id
        existing.match_type = match_type
        return existing

    link = StaffLink(
        ad_username=ad_username,
        google_email=google_email,
        paxton_id=paxton_id,
        match_type=match_type,
    )
    db.add(link)
    await db.flush()
    return link
