"""Staff module schemas — typed request/response models."""

from pydantic import BaseModel


class OnboardRequest(BaseModel):
    first_name: str
    last_name: str
    building: str
    role_type: str
    title: str | None = None
    start_date: str | None = None
    state_id: str | None = None
    phone: str | None = None
    room_number: str | None = None
    needs_sis: bool = False
    notes: str | None = None


class OffboardRequest(BaseModel):
    first_name: str
    last_name: str
    building: str
    existing_email: str | None = None
    existing_ad_username: str | None = None
    notes: str | None = None


class StaffRequestResponse(BaseModel):
    id: int
    request_type: str
    first_name: str
    last_name: str
    building: str
    role_type: str
    status: str
    submitted_by: str | None
    submitted_at: str | None


# ── Staff action schemas ─────────────────────────────────────────────────

class UpdatePaxtonAccessRequest(BaseModel):
    paxton_id: int
    access_level_id: int


class GoogleGroupRequest(BaseModel):
    email: str
    group_email: str


class DisableRequest(BaseModel):
    username: str


class IgnoreRequest(BaseModel):
    username: str
    display_name: str = ""
    reason: str = ""


class MoveADRequest(BaseModel):
    username: str
    new_ou_dn: str


class UpdateTitleRequest(BaseModel):
    username: str
    title: str


class ConfirmLinkRequest(BaseModel):
    ad_username: str
    paxton_id: int
    google_email: str = ""
