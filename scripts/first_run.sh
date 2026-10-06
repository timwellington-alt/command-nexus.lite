#!/usr/bin/env bash
# Command Nexus (lite) — first-run bootstrap.
#
# Idempotent: safe to re-run. Only generates secrets that don't
# already exist. Never overwrites existing values.
set -euo pipefail

cd "$(dirname "$0")/.."

# ── Docker socket preflight ────────────────────────────────────────
# install_prereqs.sh adds the user to the docker group, but Linux
# doesn't refresh group membership on an already-running shell. If
# this script was run before 'newgrp docker' / re-login, docker calls
# will fail with "permission denied on /var/run/docker.sock".
#
# Fix transparently: if the user IS in the docker group but the
# current shell didn't pick it up, re-exec self via sg(1) which
# activates the group for just this subprocess. User never sees the
# error.
if ! docker info >/dev/null 2>&1; then
    if id -nG "${USER:-$(whoami)}" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        if [ "${_FIRST_RUN_REEXECED:-0}" = "1" ]; then
            echo "ERROR: docker socket still denied even after sg docker — is dockerd running?" >&2
            echo "       Try: sudo systemctl start docker" >&2
            exit 1
        fi
        echo "docker group not active in this shell — re-executing via 'sg docker'…"
        export _FIRST_RUN_REEXECED=1
        exec sg docker -c "$0 $*"
    fi
    # Not in group at all: tell them to run install_prereqs
    cat >&2 <<EOF
ERROR: can't talk to the docker daemon at /var/run/docker.sock.

Likely causes:
  • You haven't run the prereq installer yet. Run:
        sudo ./scripts/install_prereqs.sh
  • Docker daemon isn't running:
        sudo systemctl start docker
  • You're not in the docker group:
        groups | grep docker  (should list it)

Fix the underlying issue, then re-run this script.
EOF
    exit 1
fi

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

# Pre-create bind-mount source directories. Git doesn't track empty
# dirs so these are missing on a fresh clone; Docker tolerates missing
# bind sources on most hosts but fails on some rootless / overlayfs
# setups with "invalid mount config for type bind".
mkdir -p data/photos data/exports

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
echo "== Secrets + bootstrap written =="
echo

# ── Bring the stack up ─────────────────────────────────────────────
# Default-yes prompt. Say N if you plan to layer a TLS overlay
# (docker-compose.caddy.yml, docker-compose.local.yml, etc.) — in
# that case you'll run the overlay-aware 'docker compose -f ... up'
# yourself after this script exits.
echo "Ready to bring the stack up with the base compose (HTTP on :8080)."
echo "Pick N if you plan to use a TLS overlay (Caddy/DNS-01/local) —"
echo "you can bring it up later with the overlay compose command."
read -p "Build + start containers now? [Y/n] " do_up
case "$(printf '%s' "$do_up" | tr '[:upper:]' '[:lower:]')" in
    n|no)
        echo
        echo "== First-run complete (containers NOT started) =="
        echo
        echo "When ready, bring the stack up with:"
        echo "    docker compose up -d --build"
        echo "    docker compose exec api alembic upgrade head"
        echo
        exit 0
        ;;
esac

echo
echo "-- building images + starting stack --"
# Pipe build output through so operators see progress on a cold build
# (image compilation can take several minutes on first run).
docker compose up -d --build

echo
echo "-- waiting for postgres to be ready --"
# Compose's depends_on condition handles the API wait, but we need
# postgres explicitly before running migrations.
for i in $(seq 1 60); do
    if docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-nexus_admin}" >/dev/null 2>&1; then
        echo "    postgres ready"
        break
    fi
    sleep 1
    if [ "$i" = "60" ]; then
        echo "ERROR: postgres didn't become ready within 60s — check 'docker compose logs postgres'" >&2
        exit 1
    fi
done

echo
echo "-- running database migrations --"
docker compose exec -T api alembic upgrade head

echo
echo "-- waiting for API to be healthy --"
for i in $(seq 1 60); do
    status="$(docker inspect --format='{{.State.Health.Status}}' nexus-lite-api 2>/dev/null || echo starting)"
    if [ "$status" = "healthy" ]; then
        echo "    api healthy"
        break
    fi
    sleep 2
    if [ "$i" = "60" ]; then
        echo "WARN: API didn't go healthy within 2 min — check 'docker compose logs api'" >&2
        echo "      (continuing anyway — it may still come up)" >&2
        break
    fi
done

# ── Login details ──────────────────────────────────────────────────
# Prefer the DOMAIN from .env if set to a real value, otherwise fall
# back to the LAN IP for the http://<ip>:8080 landing.
host_hint="$DOMAIN"
if [ -z "$host_hint" ] || [ "$host_hint" = "nexus.yourdistrict.org" ]; then
    host_hint="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -z "$host_hint" ] && host_hint="<this-vm-ip>"
    url="http://${host_hint}:8080/"
else
    url="https://${host_hint}/"
fi

echo
echo "================================================================"
echo "  Command Nexus (lite) is up."
echo "================================================================"
echo
echo "  Open:  $url"
echo
if is_true "$LOCAL_AUTH_ENABLED" && [ ! -s secrets/bootstrap_local_admin ]; then
    echo "  Sign in as:  admin@local / changeme123!"
    echo "  (You'll be forced to pick a new password on first sign-in.)"
elif is_true "$LOCAL_AUTH_ENABLED"; then
    echo "  Sign in as:  (the local admin email + password you entered)"
fi
if is_true "$GOOGLE_AUTH_ENABLED" && [ -n "$admin_email" ]; then
    echo "  Or Google SSO as: $admin_email"
fi
echo
echo "  Tail logs:   docker compose logs -f api"
echo "  Stop stack:  docker compose down"
echo
