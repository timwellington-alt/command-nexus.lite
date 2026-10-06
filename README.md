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
- Google Workspace super-admin access — needed for TWO separate things:
  the service account + DWD (backend API) AND the OAuth 2.0 Client ID
  (user Sign-In). Walkthroughs for both in `docs/DISTRICT_SETUP.pdf`
- A SIS that emails daily CSV exports — the reference implementation
  consumes PowerSchool exports; other SIS platforms need per-column
  mapping configured in Settings

## Setup

See [docs/DISTRICT_SETUP.pdf](docs/DISTRICT_SETUP.pdf) (or
[docs/DISTRICT_SETUP.md](docs/DISTRICT_SETUP.md)) for the full walkthrough
including the Google Cloud project setup, OAuth 2.0 Client creation,
and TLS options. TL;DR:

```bash
git clone https://github.com/timwellington-alt/command-nexus.lite.git command-nexus-lite
cd command-nexus-lite
sudo ./scripts/install_prereqs.sh        # Docker + Compose + deps
cp .env.example .env
$EDITOR .env
cp <path-to-your-service-account>.json secrets/google_service_account.json
./scripts/first_run.sh                    # prompts for OAuth client ID + secret
docker compose up -d                      # or use a TLS override, see below
```

Then log in at `https://<your-domain>/` with the admin email you seeded.

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

## Local auth (optional, feature-flagged)

Secondary login path for break-glass admin, Microsoft 365 shops, or
service accounts that need to call Nexus's API without Google SSO.
Off by default.

Enable in `.env`:
```
LOCAL_AUTH_ENABLED=true
```

Then re-run `scripts/first_run.sh` — it'll prompt for an initial
local admin email + password (min 12 chars) and seed them. Subsequent
accounts are created from the Settings → Local accounts admin UI (or
POST to `/api/local-users`).

Technical details:
- Passwords hashed with argon2id (OWASP-recommended defaults)
- Per-email failure lockout: 5 attempts within 15 min → 15 min lockout
- Session cookie identical to the Google flow — downstream middleware
  can't tell the two apart
- All login attempts audit-logged (success + failure)
- When flag is off: zero routes registered, zero attack surface

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

## Known issues (first-run)

**1. Database bootstrap:** `alembic upgrade head` currently expects a
few base tables to pre-exist. First-run workaround:

```bash
# After `docker compose up -d`, before anything else:
docker exec nexus-lite-api python -c "
import asyncio
from app.db.engine import engine, Base
import app.db.models                             # noqa
import app.modules.staff.models                  # noqa
import app.modules.roster.models                 # noqa
import app.modules.staff.provisioning_profiles   # noqa
async def go():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
asyncio.run(go())
"
docker exec nexus-lite-api alembic stamp head
docker exec nexus-lite-api python -m app.db.seeds
```

Once that runs, the stack boots cleanly on subsequent restarts. A
future release will fold this into `scripts/first_run.sh`.

**2. Secrets file permissions:** Docker Compose `file:`-based secrets
mount into the container owned by root with the host's permission
bits preserved. `scripts/first_run.sh` chmods secrets to 600, but the
api container runs as uid 999 and can't read them. Workaround: after
`first_run.sh`, chmod 644 the secret files. Fix pending — compose
`uid: "999"` on each secret definition is the proper solution.

## License

Shared as-is with no warranty. Use it, fork it, modify it. PRs welcome
but not expected.
