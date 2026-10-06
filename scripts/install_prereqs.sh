#!/usr/bin/env bash
# Command Nexus (lite) — host prerequisite installer.
#
# Installs Docker Engine + Compose plugin + git + curl on Ubuntu/Debian
# or RHEL/Rocky/AlmaLinux. Idempotent — safe to re-run. Needs sudo /
# root. After it finishes, log out + log back in so the docker group
# membership takes effect, then follow docs/DISTRICT_SETUP.pdf.
set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { printf "${GREEN}[nexus-lite]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[nexus-lite]${NC} %s\n" "$*"; }
fail() { printf "${RED}[nexus-lite]${NC} %s\n" "$*" >&2; exit 1; }

[ "$EUID" -eq 0 ] || fail "Run as root or with sudo: sudo $0"

if [ ! -r /etc/os-release ]; then
    fail "Can't detect OS — /etc/os-release missing"
fi
. /etc/os-release

case "$ID" in
    ubuntu|debian) DISTRO="debian" ;;
    rhel|rocky|almalinux|centos|fedora) DISTRO="rhel" ;;
    *) fail "Unsupported OS: $ID. Supported: Ubuntu, Debian, RHEL, Rocky, Alma, CentOS, Fedora." ;;
esac
log "Detected $PRETTY_NAME ($DISTRO family)"

INVOKING_USER="${SUDO_USER:-$USER}"
if [ "$INVOKING_USER" = "root" ]; then
    warn "No non-root user detected via SUDO_USER — won't add anyone to the docker group."
    warn "Re-run via 'sudo ./scripts/install_prereqs.sh' from the user that will run docker commands."
fi

# ── Install base packages ──────────────────────────────────────────
log "Installing base packages (git, curl, ca-certificates)…"
if [ "$DISTRO" = "debian" ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        git curl ca-certificates gnupg lsb-release
else
    dnf install -y -q git curl ca-certificates
fi

# ── Install Docker Engine ──────────────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker --version | grep -qE 'version 2[4-9]|version [3-9][0-9]'; then
    log "Docker Engine $(docker --version | awk '{print $3}' | tr -d ,) already installed — skipping"
else
    log "Installing Docker Engine from the official repository…"
    if [ "$DISTRO" = "debian" ]; then
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/$ID/gpg" \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $CODENAME stable" \
          > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    else
        dnf -y config-manager --add-repo "https://download.docker.com/linux/$ID/docker-ce.repo"
        dnf install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
fi

# ── Enable + start the daemon ──────────────────────────────────────
log "Enabling + starting the Docker daemon…"
systemctl enable --now docker

# ── Add the invoking user to the docker group ──────────────────────
if [ "$INVOKING_USER" != "root" ]; then
    if id -nG "$INVOKING_USER" | grep -qw docker; then
        log "$INVOKING_USER already in docker group — skipping"
    else
        log "Adding $INVOKING_USER to the docker group…"
        usermod -aG docker "$INVOKING_USER"
        warn "You MUST log out and log back in (or 'newgrp docker') before docker commands work for $INVOKING_USER."
    fi
fi

# ── Sanity-check ───────────────────────────────────────────────────
log "Verifying installation…"
docker --version
docker compose version

# Verify the invoking user can actually hit the docker socket. If they
# were just added to the group, they almost certainly can't — their
# current session's credentials don't include the new group. Call that
# out loudly with the exact command needed instead of letting them
# discover it at `docker compose up` time.
NEEDS_RELOGIN=0
if [ "$INVOKING_USER" != "root" ]; then
    if ! sudo -n -u "$INVOKING_USER" docker info >/dev/null 2>&1; then
        NEEDS_RELOGIN=1
    fi
fi

if [ "$NEEDS_RELOGIN" = "1" ]; then
    cat <<EOF

${RED}================================================================
IMPORTANT — your current shell CANNOT talk to Docker yet.
================================================================${NC}

You were added to the 'docker' group, but Linux doesn't refresh group
membership on an already-running shell. If you run 'docker compose up'
right now you'll get:

    permission denied while trying to connect to the Docker daemon
    socket at unix:///var/run/docker.sock

Fix with EITHER:

  ${YELLOW}• Log out and log back in${NC} (cleanest — every future shell gets it)

  ${YELLOW}• Or activate the group in this terminal only:${NC}
        newgrp docker

  ${YELLOW}• Or prefix docker commands with sudo${NC} (works but noisy)

Verify with:  ${GREEN}docker ps${NC}  (should list containers, not error)

EOF
fi

cat <<EOF

${GREEN}================================================================
Prerequisites installed.
================================================================${NC}

Next steps (as ${INVOKING_USER:-your deploy user}):

  1. Confirm docker works WITHOUT sudo for your user:
       docker ps
     If you get a permission error, run 'newgrp docker' or re-login
     first — see the IMPORTANT box above.

  2. Clone this repo if you haven't already:
       git clone https://github.com/timwellington-alt/command-nexus.lite.git command-nexus-lite
       cd command-nexus-lite

  3. Configure the stack:
       cp .env.example .env
       \$EDITOR .env
       cp /path/to/your/google-service-account.json \\
          secrets/google_service_account.json

  4. Run first-run — generates secrets, builds images, starts the
     stack, runs migrations, and prints the login URL + credentials:
       ./scripts/first_run.sh

  5. Follow docs/DISTRICT_SETUP.pdf for Google Workspace service
     account setup + Settings-page configuration.

EOF
