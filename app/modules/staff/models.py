"""
Staff module data models.

Workflow state is relational — not opaque JSON blobs.
Each workflow run has discrete steps with individual outcomes.
Staff directory cached from Google Workspace (source of truth).
"""

from datetime import datetime
from sqlalchemy import (
    String, Boolean, DateTime, Text, Integer, ForeignKey, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.engine import Base


class StaffRequest(Base):
    """Onboard or offboard request submitted by HR or admin."""
    __tablename__ = "staff_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "onboard" | "offboard"

    # Person info
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    building: Mapped[str] = mapped_column(String(20), nullable=False)  # PHS, PES, EPE, CO, DIST
    role_type: Mapped[str] = mapped_column(String(30), nullable=False)  # teacher, admin, classified, tech, sub
    title: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[str | None] = mapped_column(String(20))
    state_id: Mapped[str | None] = mapped_column(String(50))
    phone: Mapped[str | None] = mapped_column(String(30))
    room_number: Mapped[str | None] = mapped_column(String(20))
    needs_sis: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    # Photo
    photo_path: Mapped[str | None] = mapped_column(String(500))

    # For offboarding — reference to existing accounts
    existing_email: Mapped[str | None] = mapped_column(String(255))
    existing_ad_username: Mapped[str | None] = mapped_column(String(255))

    # Workflow status
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending → provisioning → complete | partial | cancelled

    # Tracking
    submitted_by: Mapped[str | None] = mapped_column(String(255))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    provisioned_by: Mapped[str | None] = mapped_column(String(255))
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # SIS confirmation
    sis_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    sis_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    workflow_runs: Mapped[list["StaffWorkflowRun"]] = relationship(back_populates="request", cascade="all, delete-orphan")


class StaffWorkflowRun(Base):
    """A single execution attempt of a staff request workflow."""
    __tablename__ = "staff_workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(Integer, ForeignKey("staff_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    run_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "onboard" | "offboard"
    status: Mapped[str] = mapped_column(String(20), default="running")  # running → complete | partial | failed
    started_by: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)

    # Relationships
    request: Mapped["StaffRequest"] = relationship(back_populates="workflow_runs")
    steps: Mapped[list["StaffWorkflowStep"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class StaffWorkflowStep(Base):
    """Individual step within a workflow run. Each system is a separate step."""
    __tablename__ = "staff_workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("staff_workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(50), nullable=False)  # "google", "ad", "paxton", "notification"
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending → running → success | failed | skipped
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    before_state: Mapped[str | None] = mapped_column(Text)  # JSON
    after_state: Mapped[str | None] = mapped_column(Text)   # JSON
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    # Result data (e.g. created email, username)
    result_data: Mapped[str | None] = mapped_column(Text)  # JSON

    # Relationships
    run: Mapped["StaffWorkflowRun"] = relationship(back_populates="steps")


class StaffLink(Base):
    """Confirmed cross-system identity link (e.g. AD ↔ Paxton)."""
    __tablename__ = "staff_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ad_username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    paxton_id: Mapped[int | None] = mapped_column(Integer, index=True)
    google_email: Mapped[str | None] = mapped_column(String(255), index=True)
    match_type: Mapped[str] = mapped_column(String(20), default="auto")  # auto, fuzzy, manual
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StaffIgnore(Base):
    """
    Manual override for the staff matching logic.

    Two kinds:
        confirmed   — IT vouches for this account as real staff even
                      though no HR row or building roster claims them.
                      Stays in staff_directory, labeled match_state='override'.
        non_person  — service account, shared mailbox, vendor, etc.
                      Filtered out of staff_directory entirely so it
                      never reaches the deprovision queue or any UI.

    `username` holds the email address. `reason` is the optional note
    IT can leave for future reference. `restored_at` is a soft-delete
    marker — non-null means the override has been revoked.
    """
    __tablename__ = "staff_ignores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="non_person", index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    ignored_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StaffDirectoryEntry(Base):
    """Cached staff member from Google Workspace — source of truth for staff identity."""
    __tablename__ = "staff_directory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(100))
    org_unit: Mapped[str | None] = mapped_column(String(500))
    building: Mapped[str | None] = mapped_column(String(50), index=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, suspended
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login: Mapped[str | None] = mapped_column(String(50))
    google_id: Mapped[str | None] = mapped_column(String(100))
    # Confirmation status assigned during sync_staff_directory:
    #   hr_match     — confirmed by HR sheet (email or nickname-aware name)
    #   roster_match — confirmed by a building room roster
    #   override     — manually confirmed via staff_ignores (kind='confirmed')
    #   unmatched    — nothing claims this account → deprovision queue candidate
    # (non_person rows are dropped from staff_directory entirely.)
    match_state: Mapped[str | None] = mapped_column(String(20), index=True)
    # JSON list of lowercased alias emails from Google Workspace.
    # Populated by staff_sync_job so HR-diff logic can match against
    # maiden names / legacy addresses without re-fetching from Google.
    google_aliases: Mapped[str | None] = mapped_column(Text)
    # HR Notes column free text — e.g. "On LOA", "On FMLA", "New",
    # "Née: Maiden". Copied from hr_staff_cache when the account is
    # hr_matched so the directory doesn't need a second join.
    hr_notes: Mapped[str | None] = mapped_column(Text)
    # Derived LOA indicator. True when hr_notes matches any leave
    # keyword. Populated by staff_sync_job.
    on_leave: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # True when the HR-matched Google account's primary email
    # local-part doesn't match <first>.<last> based on HR's current
    # name (typically caught via a "Née: Maiden" Notes entry where
    # Google still uses the maiden name as primary).
    email_mismatch: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 2FA / 2-Step Verification status from Google Workspace.
    # `is_enrolled_in_2sv`: user has set up 2SV.
    # `is_enforced_in_2sv`: admin has required them to enroll.
    # Both nullable so we can tell "haven't fetched" apart from
    # "definitely not enrolled" — matters on fresh installs before
    # the first staff sync completes.
    is_enrolled_in_2sv: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_enforced_in_2sv: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaxtonUserCache(Base):
    """Cached Paxton cardholder data — synced 2x daily by worker."""
    __tablename__ = "paxton_user_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paxton_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    department: Mapped[str | None] = mapped_column(String(255))
    department_id: Mapped[int | None] = mapped_column(Integer)
    access_levels: Mapped[str | None] = mapped_column(Text)  # JSON array of level IDs
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pin: Mapped[str | None] = mapped_column(String(50))
    activate_date: Mapped[str | None] = mapped_column(String(50))
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ADUserCache(Base):
    """Cached Active Directory user data — synced 2x daily by worker."""
    __tablename__ = "ad_user_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(255))
    ou: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    groups: Mapped[str | None] = mapped_column(Text)  # JSON array of group names
    last_logon: Mapped[str | None] = mapped_column(String(50))
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StaffReconciliation(Base):
    """Pre-computed cross-system reconciliation — one row per staff member.

    Populated by the reconciliation job after each data sync.
    The directory and profile endpoints read from this table directly.
    """
    __tablename__ = "staff_reconciliation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identity (from Google — source of truth)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(100))
    building: Mapped[str | None] = mapped_column(String(50), index=True)
    org_unit: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(50))
    google_status: Mapped[str] = mapped_column(String(20), default="active")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login: Mapped[str | None] = mapped_column(String(50))
    google_id: Mapped[str | None] = mapped_column(String(100))

    # Matched IDs
    paxton_id: Mapped[int | None] = mapped_column(Integer)
    ad_username: Mapped[str | None] = mapped_column(String(255))
    extension: Mapped[str | None] = mapped_column(String(20))
    room: Mapped[str | None] = mapped_column(String(50))
    room_assignment: Mapped[str | None] = mapped_column(String(255))
    room_floor: Mapped[str | None] = mapped_column(String(50))
    room_building: Mapped[str | None] = mapped_column(String(50))
    is_esc: Mapped[bool] = mapped_column(Boolean, default=False)
    hr_email: Mapped[str | None] = mapped_column(String(255))

    # Status flags
    google_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    ad_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sis_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hr_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    paxton_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    has_paxton_photo: Mapped[bool] = mapped_column(Boolean, default=False)

    # Role
    role_type: Mapped[str | None] = mapped_column(String(50))
    hr_position: Mapped[str | None] = mapped_column(String(255))
    hr_school: Mapped[str | None] = mapped_column(String(100))
    hr_classification: Mapped[str | None] = mapped_column(String(100))
    hr_notes: Mapped[str | None] = mapped_column(Text)
    on_leave: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    email_mismatch: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Mirror of staff_directory 2FA fields — populated by
    # run_staff_reconciliation. See feedback comment on the
    # StaffDirectoryEntry.is_enrolled_in_2sv column above.
    is_enrolled_in_2sv: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_enforced_in_2sv: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Match confidence
    paxton_match_method: Mapped[str | None] = mapped_column(String(30))
    ad_match_method: Mapped[str | None] = mapped_column(String(30))
    phone_match_method: Mapped[str | None] = mapped_column(String(30))
    room_match_method: Mapped[str | None] = mapped_column(String(30))

    # Names from each system (for mismatch detection)
    paxton_name: Mapped[str | None] = mapped_column(String(255))
    ad_display_name: Mapped[str | None] = mapped_column(String(255))
    phone_caller_id: Mapped[str | None] = mapped_column(String(255))

    # Computed issues (JSON text)
    name_sync_issues: Mapped[str | None] = mapped_column(Text)
    room_ext_mismatch: Mapped[str | None] = mapped_column(Text)

    # Door access
    last_door_time: Mapped[str | None] = mapped_column(String(50))
    last_door_name: Mapped[str | None] = mapped_column(String(255))
    last_door_building: Mapped[str | None] = mapped_column(String(50))

    # Phone (static, not live)
    phone_registered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    phone_ip: Mapped[str | None] = mapped_column(String(50))
    phone_model: Mapped[str | None] = mapped_column(String(100))

    # Match confirmation state — copied from staff_directory.match_state
    # so directory queries can filter without joining. Possible values:
    #   hr_match | roster_match | override | unmatched | None (legacy)
    match_state: Mapped[str | None] = mapped_column(String(20), index=True)

    # Meta
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OnboardingToken(Base):
    """
    Signed single-use URL used by new hires to fill in their own info.

    Admin generates the token, the recipient POSTs to /onboard/{token},
    submit writes to the target building's NexusData room-roster tab
    AND inserts a ``staff_queue`` row for provisioning (held in
    ``pending_review`` so an admin verifies before commit).

    The token IS the authentication — no session — so short TTL,
    single-use enforcement, and audit are the safety rails.
    """
    __tablename__ = "onboarding_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    building: Mapped[str] = mapped_column(String(20), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    single_use: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    preset_room: Mapped[str | None] = mapped_column(String(50))
    preset_title: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    # status: pending | submitted | expired | revoked
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_data: Mapped[dict | None] = mapped_column(JSONB)
    resulting_queue_id: Mapped[int | None] = mapped_column(Integer)


class HRStaffCache(Base):
    """Cached HR staff data from SMB/Sheets — synced 2x daily by worker."""
    __tablename__ = "hr_staff_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    name: Mapped[str | None] = mapped_column(String(255), index=True)
    position: Mapped[str | None] = mapped_column(String(255))
    school: Mapped[str | None] = mapped_column(String(100))
    classification: Mapped[str | None] = mapped_column(String(100))
    tab_source: Mapped[str | None] = mapped_column(String(100))
    # Free-text HR Notes column. Tracks LOA ("On LOA", "On FMLA"),
    # new-hire status, and name-change signals ("Née: Maiden").
    notes: Mapped[str | None] = mapped_column(Text)
    # Ohio state teacher certification number (HR MASTER col N). Only
    # populated for `Cert` classification staff.
    cert_number: Mapped[str | None] = mapped_column(String(50))
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
