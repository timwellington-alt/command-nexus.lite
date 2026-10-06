"""Roster module schemas — typed request/response models."""

from typing import Literal

from pydantic import BaseModel, Field


class RosterImportRequest(BaseModel):
    source: Literal["csv", "clever", "manual"]
    filename: str | None = None


class GuidanceQueueCreate(BaseModel):
    student_id: int
    category: Literal["enrollment", "withdrawal", "schedule_change", "account_issue", "other"]
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    notes: str | None = None


class GuidanceQueueUpdate(BaseModel):
    status: Literal["open", "in_progress", "resolved", "deferred"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    notes: str | None = None
    assigned_to: str | None = None


class StudentAccountProvision(BaseModel):
    student_id: int
    request_id: str = Field(
        ...,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Caller-supplied idempotency key for dedup",
    )


class ConfirmStudentIdRequest(BaseModel):
    student_id: str
    google_email: str


class IgnoreEmailRequest(BaseModel):
    email: str
    reason: str = ""


class ResolveDuplicateRequest(BaseModel):
    duplicate_email: str
    correct_email: str


class RevertGoogleRequest(BaseModel):
    revert_to: str  # ISO datetime string — revert all changes AFTER this point


class ResolveAccountReviewRequest(BaseModel):
    """IT confirms an account_review match is correct."""
    pass  # change_id comes from URL path
