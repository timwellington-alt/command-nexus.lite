#!/usr/bin/env bash
# Command Nexus (lite) — first-run bootstrap.
#
# Idempotent: safe to re-run. Only generates secrets that don't
# already exist. Never overwrites existing values.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Command Nexus first-run =="
echo

if [ ! -f .env ]; then
    echo "ERROR: .env not found. Copy .env.example to .env and edit it first." >&2
    exit 1
fi

# Source .env + apply the SAME defaults config.py uses, so an older
# .env missing the auth flags doesn't accidentally land in a different
# branch than the running API will.
set -a; # shellcheck disable=SC1091
source .env
set +a
LOCAL_AUTH_ENABLED="${LOCAL_AUTH_ENABLED:-true}"
GOOGLE_AUTH_ENABLED="${GOOGLE_AUTH_ENABLED:-false}"

is_true() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

echo "Auth mode:"
if is_true "$LOCAL_AUTH_ENABLED"; then echo "  local: ON"; else echo "  local: off"; fi
if is_true "$GOOGLE_AUTH_ENABLED"; then echo "  google: ON"; else echo "  google: off"; fi
if ! is_true "$LOCAL_AUTH_ENABLED" && ! is_true "$GOOGLE_AUTH_ENABLED"; then
    echo "ERROR: both auth modes are disabled — nobody could log in. Enable at least one in .env." >&2
    exit 1
fi
echo

mkdir -p secrets

# Generate any missing secret files. Existing files are left alone so
# a re-run doesn't rotate credentials Postgres already initialized on.
gen_secret() {
    local path="$1"; local generator="$2"
    if [ ! -s "$path" ]; then
        eval "$generator" > "$path"
        chmod 600 "$path"
        echo "  generated $path"
    else
        echo "  keeping existing $path"
    fi
}

echo "-- secrets --"
gen_secret secrets/app_secret_key           "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
gen_secret secrets/postgres_password        "python3 -c 'import secrets; print(secrets.token_urlsafe(24))'"
gen_secret secrets/redis_password           "python3 -c 'import secrets; print(secrets.token_urlsafe(24))'"
gen_secret secrets/settings_encryption_key  "python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"

# Google OAuth client (for Sign-in-with-Google) — only when flag is on.
if is_true "$GOOGLE_AUTH_ENABLED"; then
    if [ ! -s secrets/google_client_id ]; then
        echo
        echo "-- Google OAuth client (GOOGLE_AUTH_ENABLED=true) --"
        echo "  Create one at https://console.cloud.google.com/apis/credentials"
        echo "  → Create Credentials → OAuth client ID → Web application"
        echo "  → Authorized redirect URI: https://<your-domain>/auth/callback"
        echo "  → copy the Client ID (ends in .apps.googleusercontent.com)"
        read -p "  Paste it here: " gci
        printf '%s' "$gci" > secrets/google_client_id
        chmod 600 secrets/google_client_id
    fi
    if [ ! -s secrets/google_client_secret ]; then
        echo
        echo "  Paste the matching Client Secret (shown once when you created"
        echo "  the Client ID above; you can regenerate from the Credentials page)."
        read -sp "  Client Secret: " gcs; echo
        printf '%s' "$gcs" > secrets/google_client_secret
        chmod 600 secrets/google_client_secret
    fi
    if [ ! -s secrets/google_service_account.json ]; then
        echo
        echo "  WARN: secrets/google_service_account.json is missing." >&2
        echo "  If you plan to use Google backend features (Directory API,"      >&2
        echo "  Gmail attendance ingest, Sheets custom-sections sync), copy"    >&2
        echo "  your service-account key JSON into that path before start."     >&2
    fi
fi

# Admin bootstrap. If Google SSO is on, seed a Google admin email. If
# local auth is on, offer to seed a local admin (defaults are OK too —
# the API creates admin@local on first boot if nothing's seeded).
echo
echo "-- admin bootstrap --"

admin_email=""
if is_true "$GOOGLE_AUTH_ENABLED"; then
    read -p "Google admin email (must exist in your Workspace): " admin_email
    if [ -n "$admin_email" ]; then
        echo "$admin_email" > secrets/bootstrap_admin_email
        chmod 600 secrets/bootstrap_admin_email
    fi
fi

if is_true "$LOCAL_AUTH_ENABLED"; then
    echo
    echo "  Local admin seed (optional)."
    echo "  Press ENTER on both prompts to skip — the API will then auto-create"
    echo "  admin@local / changeme123! on first boot (forced password change)."
    read -p "  Local admin email: " local_email
    read -sp "  Local admin password (≥ 12 chars, or empty to skip): " local_pw; echo
    if [ -n "$local_email" ] && [ "${#local_pw}" -ge 12 ]; then
        printf '%s\n%s' "$local_email" "$local_pw" > secrets/bootstrap_local_admin
        chmod 600 secrets/bootstrap_local_admin
        echo "  seed written to secrets/bootstrap_local_admin"
        echo "  (the api container applies it on first boot, then removes the file)"
    elif [ -n "$local_email" ] || [ -n "$local_pw" ]; then
        echo "  (incomplete input — skipping local seed, default admin will be used)" >&2
    else
        echo "  (no local admin seeded — default admin@local will be used)"
    fi
fi

echo
echo "== First-run complete =="
echo
echo "Next steps:"
echo "  1. docker compose up -d"
echo "  2. Open https://\$DOMAIN/ and sign in:"
if is_true "$LOCAL_AUTH_ENABLED" && [ ! -s secrets/bootstrap_local_admin ]; then
    echo "     • Default local admin: admin@local / changeme123!"
    echo "       (You'll be forced to pick a new password immediately.)"
elif is_true "$LOCAL_AUTH_ENABLED"; then
    echo "     • Local admin: (the email + password you just entered)"
fi
if is_true "$GOOGLE_AUTH_ENABLED" && [ -n "$admin_email" ]; then
    echo "     • Google SSO as: $admin_email"
fi
echo
