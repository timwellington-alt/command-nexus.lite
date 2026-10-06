"""
Roster module data models.

Student data is the highest-sensitivity tier in Command Nexus (FERPA/COPPA).
All access must be scoped by building and audited.

T0.6: Student photos are NOT implemented — blocked pending district privacy policy.
"""

from datetime import datetime
from sqlalchemy import (
    String, Boolean, DateTime, Text, Integer, ForeignKey, func, Date,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.engine import Base


class RosterImport(Base):
    """Record of a roster import execution — replay-safe and traceable."""
    __tablename__ = "roster_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # "clever", "csv", "manual"
    filename: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending → running → complete | failed
    total_records: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    snapshots: Mapped[list["RosterSnapshot"]] = relationship(back_populates="import_record", cascade="all, delete-orphan")


class RosterSnapshot(Base):
    """
    Student record snapshot — current state after import.

    This is the authoritative student directory. Each import overwrites
    the snapshot for that student (SIS ID is the stable key).

    Field tiers per T0.4:
    - T1 (Identity): first_name, last_name, middle_name, sis_id, dob, email
    - T2 (Enrollment): school, grade, status, enrollment_date, withdrawal_date
    - T3 (Scheduling): via student_teachers join
    - T4 (Contacts): parent_guardian, phone, address — restricted to admin/principal/guidance
    - T5 (Account): google_status, email_compliant, issue_tags
    """
    __tablename__ = "roster_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("roster_imports.id", ondelete="SET NULL"), index=True)

    # T1: Identity
    sis_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(100))
    dob: Mapped[str | None] = mapped_column(String(20))  # stored as string, never in URLs
    email: Mapped[str | None] = mapped_column(String(255), index=True)

    # T2: Enrollment
    school: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # building/school code from SIS
    grade: Mapped[str | None] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, inactive, withdrawn, transferred
    enrollment_date: Mapped[str | None] = mapped_column(String(20))
    withdrawal_date: Mapped[str | None] = mapped_column(String(20))

    # T4: Contacts (restricted access — admin/principal/guidance only)
    parent_guardian: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(Text)

    # T5: Account state
    google_status: Mapped[str | None] = mapped_column(String(30))  # active, suspended, not_provisioned
    google_ou: Mapped[str | None] = mapped_column(String(200), index=True)  # cached org unit path
    email_compliant: Mapped[bool | None] = mapped_column(Boolean)
    issue_tags: Mapped[str | None] = mapped_column(Text)  # JSON list of tags: ["name_mismatch", "no_sid", etc.]

    # T6: SWIS PBIS demographic fields (verbatim from MetaSolutions).
    # Mappers to SWIS ID codes live in app/modules/roster/exports/swis_students.py.
    gender: Mapped[str | None] = mapped_column(String(10))            # 'M' | 'F' | 'X'
    race: Mapped[str | None] = mapped_column(String(10))              # single-letter EMIS code
    hispanic_latino: Mapped[str | None] = mapped_column(String(5))    # 'Y' | 'N'
    ell_status: Mapped[str | None] = mapped_column(String(5))         # 'Y' | 'N'
    iep_status: Mapped[str | None] = mapped_column(String(5))         # 'Y' | 'N'

    # Metadata
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    import_record: Mapped["RosterImport"] = relationship(back_populates="snapshots")
    teacher_assignments: Mapped[list["StudentTeacher"]] = relationship(back_populates="student", cascade="all, delete-orphan")
    queue_items: Mapped[list["GuidanceQueue"]] = relationship(back_populates="student", cascade="all, delete-orphan")


class StudentTeacher(Base):
    """
    Student ↔ teacher/section assignment.

    Feeds the class list feature (T10.5). Scoped by building via the
    student's school field.
    """
    __tablename__ = "student_teachers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, ForeignKey("roster_snapshots.id", ondelete="CASCADE"), nullable=False, index=True)
    teacher_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    teacher_email: Mapped[str | None] = mapped_column(String(255))
    section_name: Mapped[str | None] = mapped_column(String(100))
    homeroom: Mapped[str | None] = mapped_column(String(50))
    period: Mapped[str | None] = mapped_column(String(20))
    course_name: Mapped[str | None] = mapped_column(String(255))
    # SIS term identifier from Clever — 'year', 'SEM1', 'SEM2', 'TRI1'…
    # NULL for rows imported before term capture was added.
    term_name: Mapped[str | None] = mapped_column(String(20), index=True)
    school: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    student: Mapped["RosterSnapshot"] = relationship(back_populates="teacher_assignments")


class GuidanceQueue(Base):
    """
    Guidance counselor work queue item.

    Scoped by building — counselors see only their assigned school.
    """
    __tablename__ = "guidance_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, ForeignKey("roster_snapshots.id", ondelete="CASCADE"), nullable=False, index=True)
    school: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)  # enrollment, withdrawal, schedule_change, account_issue, other
    priority: Mapped[str] = mapped_column(String(20), default="normal")  # low, normal, high, urgent
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, in_progress, resolved, deferred
    notes: Mapped[str | None] = mapped_column(Text)
    assigned_to: Mapped[str | None] = mapped_column(String(255), index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    student: Mapped["RosterSnapshot"] = relationship(back_populates="queue_items")


class RosterChange(Base):
    """
    Individual student change detected during a roster import diff.

    Types: added, removed, transferred, grade_change, name_change,
           email_noncompliant, missing_email.
    """
    __tablename__ = "roster_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("roster_imports.id", ondelete="SET NULL"), index=True)
    student_id: Mapped[str | None] = mapped_column(String(50), index=True)  # SIS ID (string, not FK)
    change_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    school_code: Mapped[str | None] = mapped_column(String(50))
    student_name: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text)  # JSON
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    google_provisioned: Mapped[bool] = mapped_column(Boolean, default=False)
    nutrikids_queued: Mapped[bool] = mapped_column(Boolean, default=False)
    nutrikids_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GoogleChangeLog(Base):
    """
    Immutable audit record of every Google Workspace mutation made by Command Nexus.

    Actions: create_account, rename_account, set_student_id, suspend_account,
             reactivate_account, move_ou.
    """
    __tablename__ = "google_change_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)  # "system" or user email
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    student_id: Mapped[str | None] = mapped_column(String(50))
    before_state: Mapped[str | None] = mapped_column(Text)  # JSON
    after_state: Mapped[str | None] = mapped_column(Text)   # JSON
    reverted: Mapped[bool] = mapped_column(Boolean, default=False)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[str | None] = mapped_column(String(255))


class EmailIgnore(Base):
    """
    Roster email compliance whitelist.

    Emails in this list are treated as compliant regardless of format.
    Soft-delete via restored_at.
    """
    __tablename__ = "email_ignores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    ignored_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
