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
- Domain name pointing to the host + TLS cert (Let's Encrypt or Cloudflare Tunnel)
- Google Workspace super-admin access (for service-account + DWD setup)
- A SIS that emails daily CSV exports — the reference implementation
  consumes PowerSchool exports; other SIS platforms need per-column
  mapping configured in Settings

## Setup

See [docs/DISTRICT_SETUP.pdf](docs/DISTRICT_SETUP.pdf) (or
[docs/DISTRICT_SETUP.md](docs/DISTRICT_SETUP.md)) for the full walkthrough.
TL;DR:

```bash
git clone https://github.com/timwellington-alt/command-nexus.lite.git command-nexus-lite
cd command-nexus-lite
sudo ./scripts/install_prereqs.sh        # Docker + Compose + deps
cp .env.example .env
$EDITOR .env
cp <path-to-your-service-account>.json secrets/google_service_account.json
./scripts/first_run.sh
docker compose up -d
```

Then log in at `https://<your-domain>/` with the admin email you seeded.

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
