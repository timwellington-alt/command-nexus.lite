# Command Nexus (lite)

Self-hosted staff + student management for K-12 districts. Ships the
Staff and Roster modules plus the workflow around them: guidance
queue, ID cards, onboarding tokens, custom sections, attendance
reports, and provisioning.

## Scope

This is a focused, batteries-included template. It covers the identity
+ enrollment side of district operations (Google Workspace account
lifecycle, SIS roster ingestion, attendance digests, guidance queue
routing, printable ID cards). It does **not** cover physical access
control, phone systems, camera VMS, network ACLs, or infrastructure
monitoring — those are district-specific and are left out of the
template.

## Stack

| Container   | Image                          | Purpose                                    |
|-------------|--------------------------------|--------------------------------------------|
| `nginx`     | nginx:1.27-alpine              | Reverse proxy + TLS termination            |
| `postgres`  | postgres:16                    | Primary datastore                          |
| `redis`     | redis:7-alpine                 | Session store + job queue                  |
| `api`       | built from `app/Dockerfile`    | FastAPI web app                            |
| `worker`    | same image as `api`            | ARQ background jobs                        |
| `scheduler` | same image as `api`            | Cron-style job enqueueing                  |
| `autoheal`  | willfarrell/autoheal:1.2.0     | Restart unhealthy containers automatically |

Total working set: ~1.5 GB RAM, ~10 GB disk after 6 months of audit
logs. A 4 GB / 40 GB / 2-core VM runs this comfortably.

## Prerequisites

- Linux host with Docker Engine 24+ and Docker Compose v2
- Domain name pointing to the host (TLS setup covered below)
- Optional: a SIS that emails daily CSV exports — the reference
  implementation consumes PowerSchool exports; other SIS platforms
  need per-column mapping configured in Settings
- Optional: Google Workspace, if you want to enable:
    - Google SSO for user Sign-In (`GOOGLE_AUTH_ENABLED=true`)
    - Google backend features (Directory API, Gmail-based attendance
      ingest, Sheets custom-sections sync) — requires a service
      account + DWD regardless of SSO choice

## Setup

See [docs/DISTRICT_SETUP.pdf](docs/DISTRICT_SETUP.pdf) (or
[docs/DISTRICT_SETUP.md](docs/DISTRICT_SETUP.md)) for the full walkthrough
including the Google Cloud project setup, OAuth 2.0 Client creation,
and TLS options. TL;DR for a plain HTTP LAN deployment:

```bash
git clone https://github.com/timwellington-alt/command-nexus.lite.git command-nexus-lite
cd command-nexus-lite
sudo ./scripts/install_prereqs.sh         # Docker + Compose + deps; grants docker-socket access
cp .env.example .env
nano .env                                 # (optional) set DOMAIN or enable Google SSO — defaults boot fine
./scripts/first_run.sh                    # generates secrets, builds, runs migrations, brings stack up
```

That's it. `first_run.sh` prints the login URL and credentials at the
end. Local auth is on by default — the first login is `admin@local` /
`changeme123!`, forced to change on first use.

If `install_prereqs.sh` fails midway with an apt fetch error (transient
network to Ubuntu mirrors), rerun it — it's idempotent, and the second
pass usually completes.

If you plan to layer a TLS overlay (Caddy, DNS-01, local internal CA),
answer **N** to `first_run.sh`'s "bring the stack up now?" prompt and
run the overlay compose command yourself afterwards — see TLS section
below.

If you want Google SSO, set `GOOGLE_AUTH_ENABLED=true` in `.env` before
running `first_run.sh` and it'll prompt for the OAuth client ID +
secret. Backend Google features (Directory API, Gmail attendance,
Sheets sync) additionally need a service account at
`secrets/google_service_account.json`.

## TLS — pick one path

1. **Caddy auto-TLS** (easiest for Internet-facing; zero cert maintenance):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d
   ```
   Needs ports 80 + 443 reachable from the Internet. Caddy
   auto-provisions and auto-renews Let's Encrypt certs.

2. **nginx + certbot**: keep the default nginx proxy, run certbot on
   the host, point nginx at `/etc/letsencrypt/live/...`.

3. **Cloudflare Tunnel or your existing load balancer**: leave
   nginx HTTP-only on 8080, point your edge at it with
   `X-Forwarded-Proto: https`.

4. **Local-only / LAN deployment** (no Internet exposure):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
   ```
   Caddy's internal CA self-signs a cert for your LAN hostname.
   One-time browser warning per device (or push Caddy's root CA
   via GPO/MDM). Google OAuth still works — the callback URL is
   HTTPS even though the cert is self-signed.

5. **DNS-01 ACME** (real Let's Encrypt cert, no inbound 80/443):
   For LAN-only hosts that have outbound DNS-API access to their
   registrar. Cloudflare is the default plugin; swap in any provider
   from <https://github.com/caddy-dns>.

Full commands and tradeoffs for each path in
[docs/DISTRICT_SETUP.pdf](docs/DISTRICT_SETUP.pdf).

## Auth

**Local auth is the default** (`LOCAL_AUTH_ENABLED=true`). Users sign in
with email + password; admins managed through the Settings UI.

On first boot a **default admin** is created automatically:
- email: `admin@local`
- password: `changeme123!`
- `must_change_password=true` — the first login forces a password reset
  before anything else works

Subsequent accounts live in **Settings → Access → Local Accounts** (list,
add, reset password, enable/disable, grant/revoke admin, delete) or
via `POST /api/local-users`.

Technical defaults:
- argon2id password hashing (OWASP-recommended)
- Per-email failure lockout: 5 attempts within 15 min → 15 min lockout
- All login attempts audit-logged (success + failure)

## Google SSO (optional)

For districts that use Google Workspace and want staff to sign in
with their district Google account instead of (or in addition to) a
local account. Off by default.

Enable in `.env`:
```
GOOGLE_AUTH_ENABLED=true
```

Then set up a Google OAuth 2.0 Client — walkthrough below. When both
auth paths are enabled, the login page shows the local form at the
top + "Sign in with Google" button below.

## Google OAuth 2.0 Client — for user Sign-In

Separate from the service account (which handles backend API work).
Short version:

1. Google Cloud Console → **APIs & Services → OAuth consent screen**
   → User type **Internal** → scopes `openid`, `email`, `profile`.
2. **APIs & Services → Credentials → Create Credentials → OAuth
   client ID → Web application**.
3. **Authorized redirect URI**: `https://<your-domain>/auth/callback`
   (exact path — Nexus's OAuth router expects it there).
4. Copy the Client ID + Client Secret into
   `secrets/google_client_id` and `secrets/google_client_secret`
   (or let `first_run.sh` prompt for them).

Full walkthrough with screenshots and admin-console paths in
[docs/DISTRICT_SETUP.pdf](docs/DISTRICT_SETUP.pdf).

## Troubleshooting

**Permission denied on `/var/run/docker.sock`:** `install_prereqs.sh`
grants the invoking user immediate socket access via `setfacl` and
adds them to the docker group. If a subsequent shell still can't
reach the daemon, log out of SSH and log back in — every new login
shell picks up the group from `/etc/group`.

**"invalid mount config for type bind" on compose up:** a bind-mount
source directory is missing. `first_run.sh` pre-creates
`data/photos`, `data/exports`, and empty Google secret stubs; if you
hit this error without running `first_run.sh`, run it (it's
idempotent) or manually:
```bash
mkdir -p data/photos data/exports
touch secrets/google_client_id secrets/google_client_secret secrets/google_service_account.json
```

**API container unhealthy, log shows `No module named 'app.X.Y'`:** a
stripped-feature import leaked through the scrub. Open an issue with
the full traceback — all remaining broken imports in the lite tree
are *lazy* (inside functions), so they only fire on specific endpoints
and don't block boot. If one does block boot, the fix is almost
always a one-line import path correction.

**Database on first boot:** `first_run.sh` runs
`alembic upgrade head` inside the api container automatically. No
manual bootstrap needed.

**`asyncpg.exceptions.InvalidPasswordError`:** the postgres data
volume outlived its initial password. Postgres only honors
`POSTGRES_PASSWORD_FILE` on the very first init of an empty data
dir; any subsequent change in `secrets/postgres_password` is
ignored. Happens when the stack was partially started, torn down
without `-v`, and the secrets were regenerated. Fix on a fresh
install (no real data yet):
```bash
docker compose down -v        # drops postgres + redis volumes
docker compose up -d
docker compose exec api alembic upgrade head
```
Never run `-v` once you have real production data.

## License

Shared as-is with no warranty. Use it, fork it, modify it. PRs welcome
but not expected.
