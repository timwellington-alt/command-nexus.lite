"""Idempotent local-auth admin seeder.

Run from first_run.sh (or manually inside the api container) to
guarantee the deploy has at least one working admin login. Fails
loudly on any error — unlike the lifespan bootstrap that was
swallowed by a try/except.

Usage:
    python -m app.scripts.seed_local_admin [--email EMAIL --password PW]

With no args, seeds admin@local / changeme123! and forces a
password change on first login. With args, upserts the specified
account (useful when first_run.sh collected a custom email+pw).

Exit codes:
    0 — row exists after this run (either we inserted or it was there)
    1 — fatal error (DB unreachable, schema mismatch, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import sys


async def _run(email: str, password: str, force_change: bool) -> int:
    from sqlalchemy import text
    from app.db.engine import AsyncSessionLocal
    from app.auth.local import _hasher

    email = email.strip().lower()
    pw_hash = _hasher().hash(password)

    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                INSERT INTO local_users
                    (email, display_name, pw_hash, is_admin, created_by, must_change_password)
                VALUES (:e, :e, :p, true, 'first_run', :m)
                ON CONFLICT (email) DO UPDATE SET
                    pw_hash = EXCLUDED.pw_hash,
                    is_admin = true,
                    active = true,
                    must_change_password = EXCLUDED.must_change_password
            """).bindparams(e=email, p=pw_hash, m=force_change),
        )
        await db.commit()

        # Verify. If this returns 0 the whole flow is broken.
        row = (await db.execute(
            text("SELECT COUNT(*) FROM local_users WHERE lower(email) = :e").bindparams(e=email),
        )).scalar_one()
        if row != 1:
            print(f"ERROR: INSERT appeared to succeed but SELECT found {row} rows for {email}",
                  file=sys.stderr)
            return 1

    print(f"OK — local admin '{email}' is live"
          f"{' (must change password on first login)' if force_change else ''}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Seed a local-auth admin account")
    p.add_argument("--email", default="admin@local",
                   help="Admin email (default: admin@local)")
    p.add_argument("--password", default="changeme123!",
                   help="Initial password (default: changeme123!)")
    p.add_argument("--no-force-change", action="store_true",
                   help="Don't force a password change on first login. "
                        "Default: force change for 'admin@local', skip for custom emails.")
    args = p.parse_args()

    # Default behavior: force change when the email is the obvious
    # default; skip for custom emails (operator presumably entered the
    # password they want to actually use).
    if args.no_force_change:
        force_change = False
    else:
        force_change = args.email.strip().lower() == "admin@local"

    try:
        return asyncio.run(_run(args.email, args.password, force_change))
    except Exception as e:
        print(f"ERROR: seed_local_admin failed: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
