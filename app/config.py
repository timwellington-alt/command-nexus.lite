"""
Command Nexus v2 — Configuration provider.

Reads secrets from file mounts first (/run/secrets/<name>), falls back to
environment variables. Route handlers and templates never access secrets
directly — all access goes through this provider.
"""

import os
from functools import lru_cache
from pathlib import Path


SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))


def _read_secret(name: str, env_fallback: str = "", default: str = "") -> str:
    """Read a secret from file mount, falling back to env var."""
    secret_path = SECRETS_DIR / name
    if secret_path.is_file():
        return secret_path.read_text().strip()
    return os.environ.get(env_fallback, default)


class Settings:
    """Application settings — all config access goes through here."""

    def __init__(self):
        self.app_env = os.environ.get("APP_ENV", "development")
        self.is_production = self.app_env == "production"
        self.domain = os.environ.get("DOMAIN", "localhost")
        # Allowed domains for OAuth redirect + local-login origin check
        # (comma-separated, includes DOMAIN automatically).
        _extra = os.environ.get("ALLOWED_DOMAINS", "")
        self.allowed_domains = {self.domain} | {d.strip() for d in _extra.split(",") if d.strip()}

        # Shareable-fork UX: if DOMAIN is still at the first-boot
        # placeholder, the operator hasn't configured it yet. Rather
        # than reject every login POST with "cross-origin login POST
        # rejected", flip to permissive-origin mode so the operator
        # can click through from any LAN IP / hostname. Logged loudly
        # as a warning so a production deployer knows to set a real
        # DOMAIN before shipping.
        _PLACEHOLDER_DOMAINS = {
            "nexus.yourdistrict.org", "your-district.example.com",
            "example.com", "localhost",
        }
        # Permissive if DOMAIN is still a placeholder — regardless of
        # APP_ENV. Rationale: if DOMAIN is unconfigured, APP_ENV status
        # is unreliable too (the .env.example ships APP_ENV=production).
        # The warning is sufficient to flag it; the strict check kicks
        # back in the moment a real DOMAIN is set.
        self.origin_check_permissive = self.domain in _PLACEHOLDER_DOMAINS
        if self.origin_check_permissive:
            import logging as _l
            _l.getLogger(__name__).warning(
                "DOMAIN is still at a placeholder value (%r) — login "
                "origin check is in permissive mode. Set DOMAIN in .env "
                "(and ALLOWED_DOMAINS for any aliases) to lock this "
                "down before going public.", self.domain,
            )

        # App secret
        self.app_secret_key = _read_secret("app_secret_key", "APP_SECRET_KEY", "change-me-in-production")
        if self.is_production and self.app_secret_key == "change-me-in-production":
            raise RuntimeError(
                "FATAL: app_secret_key is set to the default value in production. "
                "Mount a Docker secret at /run/secrets/app_secret_key or set APP_SECRET_KEY."
            )

        # Database
        self.postgres_user = _read_secret("postgres_user", "POSTGRES_USER", "nexus_admin")
        self.postgres_password = _read_secret("postgres_password", "POSTGRES_PASSWORD")
        self.postgres_db = _read_secret("postgres_db", "POSTGRES_DB", "command_nexus_v2")
        self.postgres_host = os.environ.get("POSTGRES_HOST", "nexus-postgres")
        self.postgres_port = os.environ.get("POSTGRES_PORT", "5432")

        # Redis
        self.redis_password = _read_secret("redis_password", "REDIS_PASSWORD")
        self.redis_host = os.environ.get("REDIS_HOST", "nexus-redis")
        self.redis_port = os.environ.get("REDIS_PORT", "6379")

        # Local auth is the default auth path. Google SSO is a
        # feature-flagged addition for districts that use Workspace.
        # Flipped 2026-10-06: local-first is more inclusive for a
        # template that lands in Microsoft-shop districts too.
        self.local_auth_enabled = os.environ.get("LOCAL_AUTH_ENABLED", "true").lower() in ("1", "true", "yes", "on")

        # Google OAuth for user Sign-In (OPTIONAL). When off, the
        # /auth/login + /auth/callback routes aren't mounted and the
        # login page hides the "Sign in with Google" button. Google
        # Workspace is still required separately for the SERVICE
        # ACCOUNT backend features (Directory, Gmail, Sheets); that
        # dependency is unrelated to this flag.
        self.google_auth_enabled = os.environ.get("GOOGLE_AUTH_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        self.google_client_id = _read_secret("google_client_id", "GOOGLE_CLIENT_ID", "")
        self.google_client_secret = _read_secret("google_client_secret", "GOOGLE_CLIENT_SECRET", "")
        self.google_domain = os.environ.get("GOOGLE_DOMAIN", "")

        # Safety: at least one auth path must be on, else nobody can log in.
        if not self.local_auth_enabled and not self.google_auth_enabled:
            raise RuntimeError(
                "FATAL: both LOCAL_AUTH_ENABLED and GOOGLE_AUTH_ENABLED are "
                "off — nobody could log in. Enable at least one."
            )

        # Settings encryption key (for integration secrets at rest)
        self.settings_encryption_key = _read_secret(
            "settings_encryption_key",
            "SETTINGS_ENCRYPTION_KEY",
        )

        # Google service account (for Group membership checks)
        self.google_service_account_file = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE", "/run/secrets/google_service_account.json"
        )
        self.google_admin_email = os.environ.get("GOOGLE_ADMIN_EMAIL", "")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """Sync URL for Alembic migrations."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
