"""
Settings repository — DB access for integration config and feature toggles.

All settings queries go through here. No raw secrets are returned —
secret references are masked in output.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IntegrationConfig, FeatureToggle

logger = logging.getLogger(__name__)

SECRET_MASK = "\u2022" * 8  # ••••••••


async def get_integration_settings(
    db: AsyncSession,
    integration: str | None = None,
) -> list[dict]:
    """
    Get all settings for an integration (or all integrations).
    Secret references are masked — never return raw secret paths.
    """
    q = select(IntegrationConfig)
    if integration:
        q = q.where(IntegrationConfig.integration == integration)
    q = q.order_by(IntegrationConfig.integration, IntegrationConfig.key)

    result = await db.execute(q)
    return [
        {
            "integration": s.integration,
            "key": s.key,
            "value": SECRET_MASK if s.is_secret_ref else (s.value or ""),
            "is_secret_ref": s.is_secret_ref,
            "updated_by": s.updated_by,
        }
        for s in result.scalars().all()
    ]


async def upsert_integration_setting(
    db: AsyncSession,
    *,
    integration: str,
    key: str,
    value: str,
    is_secret_ref: bool = False,
    updated_by: str,
) -> bool:
    """Stage a single integration setting write. Caller must commit.
    Returns True if the value actually changed, False if it was a no-op."""
    result = await db.execute(
        select(IntegrationConfig).where(
            IntegrationConfig.integration == integration,
            IntegrationConfig.key == key,
        )
    )
    existing = result.scalar_one_or_none()

    # Encrypt secret values before storage
    store_value = value
    if is_secret_ref and value and value != SECRET_MASK:
        from app.modules.settings.encryption import encrypt_secret, decrypt_secret
        store_value = encrypt_secret(value)

    if existing:
        # Skip masked values (user didn't change the secret)
        if is_secret_ref and value == SECRET_MASK:
            return False
        # Skip if value is identical to what's already stored.
        # For secrets: decrypt the stored ciphertext and compare plaintext —
        # Fernet is non-deterministic so ciphertext comparison always differs.
        if is_secret_ref and existing.value:
            try:
                stored_plaintext = decrypt_secret(existing.value)
                if stored_plaintext == value:
                    return False
            except Exception:
                pass  # If decrypt fails, treat as changed and overwrite
        elif not is_secret_ref and existing.value == value:
            return False
        existing.value = store_value
        existing.is_secret_ref = is_secret_ref
        existing.updated_by = updated_by
        existing.updated_at = datetime.now(timezone.utc)
        return True
    elif value and value != SECRET_MASK:
        db.add(IntegrationConfig(
            integration=integration,
            key=key,
            value=store_value,
            is_secret_ref=is_secret_ref,
            updated_by=updated_by,
        ))
        return True
    # No commit here — caller owns the transaction
    return False


async def get_setting_value(
    db: AsyncSession,
    integration: str,
    key: str,
) -> str | None:
    """Get a decrypted setting value (for internal use — not for API responses).
    Secret values are decrypted transparently. Module adapters call this."""
    result = await db.execute(
        select(IntegrationConfig).where(
            IntegrationConfig.integration == integration,
            IntegrationConfig.key == key,
        )
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        return None
    if setting.is_secret_ref and setting.value:
        from app.modules.settings.encryption import decrypt_secret
        return decrypt_secret(setting.value)
    return setting.value


async def delete_integration_settings(
    db: AsyncSession,
    integration: str,
) -> int:
    """Delete all settings for an integration. Caller must commit. Returns count deleted."""
    result = await db.execute(
        delete(IntegrationConfig).where(IntegrationConfig.integration == integration)
    )
    return result.rowcount


async def get_all_integrations(db: AsyncSession) -> list[str]:
    """Get distinct integration names."""
    result = await db.execute(
        select(IntegrationConfig.integration).distinct().order_by(IntegrationConfig.integration)
    )
    return [r[0] for r in result.all()]


# ── Feature Toggles ──────────────────────────────────────────────────────

async def get_feature_toggles(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(FeatureToggle).order_by(FeatureToggle.key))
    return [
        {
            "key": t.key,
            "enabled": t.enabled,
            "description": t.description,
            "updated_by": t.updated_by,
        }
        for t in result.scalars().all()
    ]


async def set_feature_toggle(
    db: AsyncSession,
    *,
    key: str,
    enabled: bool,
    updated_by: str,
) -> None:
    """Stage a feature toggle write. Caller must commit."""
    result = await db.execute(select(FeatureToggle).where(FeatureToggle.key == key))
    toggle = result.scalar_one_or_none()
    if toggle:
        toggle.enabled = enabled
        toggle.updated_by = updated_by
        toggle.updated_at = datetime.now(timezone.utc)
    else:
        db.add(FeatureToggle(key=key, enabled=enabled, updated_by=updated_by))
    # No commit here — caller owns the transaction
