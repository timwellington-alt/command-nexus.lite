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

# Google OAuth client (for the web login flow) — separate from the
# service account. Prompted only if not already set.
if [ ! -s secrets/google_client_id ]; then
    echo
    echo "  Google OAuth 2.0 Client ID (for user Sign-In)."
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

# Service account JSON — must be provided manually; we only verify.
if [ ! -s secrets/google_service_account.json ]; then
    echo
    echo "  WARN: secrets/google_service_account.json is missing." >&2
    echo "  Copy your Google Cloud service-account key JSON into that path"  >&2
    echo "  before starting the stack, then run this script again."          >&2
fi

echo
echo "-- admin bootstrap --"
read -p "Initial admin email (must exist in your Google Workspace): " admin_email
if [ -z "$admin_email" ]; then
    echo "ERROR: admin email is required." >&2
    exit 1
fi

# Write to a bootstrap file the api container reads on first startup.
echo "$admin_email" > secrets/bootstrap_admin_email
chmod 600 secrets/bootstrap_admin_email

# ─── Local-auth seed (optional) ─────────────────────────────────
# If LOCAL_AUTH_ENABLED=true in .env, prompt for an initial local
# admin + password so a non-Google login path works immediately.
if grep -qE '^LOCAL_AUTH_ENABLED\s*=\s*(1|true|yes|on)' .env 2>/dev/null; then
    echo
    echo "-- local-auth seed --"
    echo "  LOCAL_AUTH_ENABLED detected. Seeding an initial local admin so"
    echo "  you can log in without Google SSO (useful for break-glass and"
    echo "  Microsoft 365 shops)."
    read -p "  Local admin email: " local_email
    read -sp "  Local admin password (≥ 12 chars): " local_pw; echo
    if [ -z "$local_email" ] || [ "${#local_pw}" -lt 12 ]; then
        echo "  WARN: skipping local-admin seed (email missing or password too short)" >&2
    else
        printf '%s\n%s' "$local_email" "$local_pw" > secrets/bootstrap_local_admin
        chmod 600 secrets/bootstrap_local_admin
        echo "  seed written to secrets/bootstrap_local_admin"
        echo "  (the api container applies it on first startup, then removes the file)"
    fi
fi

echo
echo "== First-run complete =="
echo
echo "Next steps:"
echo "  1. If google_service_account.json wasn't in place above, add it now."
echo "  2. docker compose up -d"
echo "  3. Open https://\$DOMAIN/ and sign in as $admin_email"
echo
