"""
Command Nexus v2 — Core database models.

All models inherit from Base (app.db.engine). Module-specific models
are defined in their module directories and imported here so Alembic
can see them for autogenerate.

This file defines platform-level models: users, roles, permissions,
audit, and integration config.
"""

from datetime import date, datetime
from sqlalchemy import (
    String, Boolean, Date, DateTime, Text, Integer, ForeignKey,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.engine import Base


# ── Users ─────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    picture: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # App-layer pointer to the voice recipient representing this user for
    # alert dispatch. No DB FK — voice module has a hard "no FK to users" rule.
    voice_recipient_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Ordered list of dashboard card IDs the user has chosen to display.
    # NULL = default layout (all cards their permissions allow, in registry order).
    dashboard_layout: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Flat dict of per-user UI preferences, namespaced by feature key.
    # First key: ``default_label_printer_id`` for chromebook label print.
    # Read-modify-write; never trust the client to send the whole dict.
    preferences: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Self-set out-of-office marker. When a routing rule's primary
    # assignee has out_of_office_until >= today, the resolver walks
    # the backup chain. Auto-expires — no cron, just a date compare.
    out_of_office_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Relationships
    user_roles: Mapped[list["UserRole"]] = relationship(back_populates="user", cascade="all, delete-orphan")


# ── Roles ─────────────────────────────────────────────────────────────────

class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    role_permissions: Mapped[list["RolePermission"]] = relationship(back_populates="role", cascade="all, delete-orphan")
    user_roles: Mapped[list["UserRole"]] = relationship(back_populates="role", cascade="all, delete-orphan")


# ── Permissions ───────────────────────────────────────────────────────────

class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_student_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    role_permissions: Mapped[list["RolePermission"]] = relationship(back_populates="permission", cascade="all, delete-orphan")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission_id: Mapped[int] = mapped_column(Integer, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)

    role: Mapped["Role"] = relationship(back_populates="role_permissions")
    permission: Mapped["Permission"] = relationship(back_populates="role_permissions")


# ── User → Role assignment with optional scope ────────────────────────────

class UserRole(Base):
    __tablename__ = "user_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True)
    scope_type: Mapped[str | None] = mapped_column(String(50))   # 'district', 'school', 'guidance_group'
    scope_value: Mapped[str | None] = mapped_column(String(100))  # '*', 'BLDG1', 'guidance-elem'
    assigned_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "role_id", "scope_type", "scope_value"),)

    user: Mapped["User"] = relationship(back_populates="user_roles")
    role: Mapped["Role"] = relationship(back_populates="user_roles")


# ── Audit Log ─────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255))
    module: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(20), default="success")  # success, denied, error
    details: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# ── Integration Config References ─────────────────────────────────────────

class IntegrationConfig(Base):
    """
    Stores integration configuration and encrypted secret values.

    Non-secret fields (is_secret_ref=False): stored as plaintext (URLs, usernames, etc.)
    Secret fields (is_secret_ref=True): stored as Fernet ciphertext — never plaintext.

    Encryption/decryption is handled transparently by settings/repository.py.
    The encryption key lives in /run/secrets/settings_encryption_key (file mount, never in DB).
    Raw secret values never appear in API responses — masked as bullets in the UI.
    """
    __tablename__ = "integration_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # 'google', 'ad', 'paxton', etc.
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    is_secret_ref: Mapped[bool] = mapped_column(Boolean, default=False)  # True = value is a secret file path, not inline
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("integration", "key"),)


# ── Feature Toggles ───────────────────────────────────────────────────────

class FeatureToggle(Base):
    __tablename__ = "feature_toggles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
