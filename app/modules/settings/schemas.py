"""
Settings schemas — typed request/response models.

Keeps raw secrets out of API responses. Integration config values
that are secret references show as masked, not the actual value.
"""

from pydantic import BaseModel


class SettingValue(BaseModel):
    integration: str
    key: str
    value: str | None = None
    is_secret_ref: bool = False


class SettingUpdate(BaseModel):
    """Batch update of settings for a single integration."""
    integration: str
    settings: dict[str, str]


class SettingResponse(BaseModel):
    integration: str
    key: str
    value: str  # Masked if is_secret_ref
    is_secret_ref: bool
    updated_by: str | None = None


class FeatureToggleUpdate(BaseModel):
    key: str
    enabled: bool


class FeatureToggleResponse(BaseModel):
    key: str
    enabled: bool
    description: str | None = None
    updated_by: str | None = None


class IntegrationTestResult(BaseModel):
    integration: str
    status: str  # "ok" | "error"
    message: str
    latency_ms: int | None = None
