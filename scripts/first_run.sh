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

# Google OAuth client (for the Sign-in-with-Google button). Prompted
# only when GOOGLE_AUTH_ENABLED=true in .env. Otherwise skipped —
# local auth is the default; Google SSO is opt-in.
if grep -qE '^GOOGLE_AUTH_ENABLED\s*=\s*(1|true|yes|on)' .env 2>/dev/null; then
    if [ ! -s secrets/google_client_id ]; then
        echo
        echo "-- Google OAuth client (GOOGLE_AUTH_ENABLED=true detected) --"
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
    # Service account JSON — needed for backend Directory/Gmail/Sheets
    # too. Must be provided manually; we only verify.
    if [ ! -s secrets/google_service_account.json ]; then
        echo
        echo "  WARN: secrets/google_service_account.json is missing." >&2
        echo "  If you plan to use Google backend features (Directory API,"      >&2
        echo "  Gmail-based attendance ingest, Sheets custom-sections sync),"    >&2
        echo "  copy your service-account key JSON into that path before"        >&2
        echo "  starting the stack."                                             >&2
    fi
fi

echo
echo "-- admin bootstrap --"
if grep -qE '^GOOGLE_AUTH_ENABLED\s*=\s*(1|true|yes|on)' .env 2>/dev/null; then
    read -p "Initial admin email (must exist in your Google Workspace): " admin_email
    if [ -z "$admin_email" ]; then
        echo "ERROR: admin email is required when Google SSO is on." >&2
        exit 1
    fi
    echo "$admin_email" > secrets/bootstrap_admin_email
    chmod 600 secrets/bootstrap_admin_email
else
    echo "  Google SSO disabled — skipping Google admin seed."
    echo "  A default local admin (admin@local / changeme123!) will be"
    echo "  created on first API boot. First login forces a password change."
fi

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
echo "  1. docker compose up -d"
if grep -qE '^GOOGLE_AUTH_ENABLED\s*=\s*(1|true|yes|on)' .env 2>/dev/null; then
    echo "  2. Open https://\$DOMAIN/ and sign in with Google as $admin_email"
    echo "     OR use the local admin (see below)."
else
    echo "  2. Open https://\$DOMAIN/ and sign in with the default local admin:"
    echo "       email:    admin@local"
    echo "       password: changeme123!"
    echo "     You'll be forced to pick a new password immediately."
fi
echo
