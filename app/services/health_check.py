"""Nexus health-check — comprehensive snapshot across DB, Redis, jobs,
integrations, alerts, and audit trail.

Usage (CLI, inside the api or worker container):
    docker exec nexus-v2-api bash -c "cd /app && PYTHONPATH=/app python -m app.services.health_check"

Also invoked from ``app.modules.operations.health_router`` for the
web surface at ``/operations/health``. Any single query can fail
without poisoning the rest — every SELECT runs in its own session
via ``try_scalar`` / ``try_all``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone


async def try_scalar(sql: str):
    from app.db.engine import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        try:
            return (await db.execute(text(sql))).scalar()
        except Exception as e:
            return e


async def try_all(sql: str):
    from app.db.engine import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        try:
            return list((await db.execute(text(sql))).mappings().all())
        except Exception as e:
            return e


async def collect() -> dict:
    """Return a structured snapshot of Nexus health. Suitable for both
    HTML rendering and CLI printing. Every section is a dict so
    partial failures don't break the whole payload."""
    from app.config import get_settings

    now = datetime.now(timezone.utc)
    out: dict = {"generated_at": now.isoformat(timespec="seconds"), "sections": {}}

    # ── Postgres ─────────────────────────────────────────────
    db_name = await try_scalar("SELECT current_database()")
    active = await try_scalar("SELECT COUNT(*) FROM pg_stat_activity WHERE state='active'")
    if isinstance(db_name, Exception):
        out["sections"]["postgres"] = {"ok": False, "error": str(db_name)}
    else:
        out["sections"]["postgres"] = {
            "ok": True, "db": db_name, "active_connections": active,
        }

    # ── Redis ────────────────────────────────────────────────
    try:
        import redis.asyncio as aioredis
        rc = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        pong = await rc.ping()
        dbsize = await rc.dbsize()
        info = await rc.info(section="stats")
        await rc.aclose()
        out["sections"]["redis"] = {
            "ok": bool(pong), "dbsize": dbsize,
            "ops_per_sec": info.get("instantaneous_ops_per_sec"),
        }
    except Exception as e:
        out["sections"]["redis"] = {"ok": False, "error": str(e)}

    # ── Job liveness ─────────────────────────────────────────
    from app.scheduler import SCHEDULES
    intervals = {name: interval for name, interval, _ in SCHEDULES}
    jh = await try_all("""
        SELECT job_name, last_success_at, last_failure_at, consecutive_failures,
               last_duration_sec, total_success, total_failure, last_error
        FROM job_health ORDER BY job_name
    """)

    if isinstance(jh, Exception):
        out["sections"]["jobs"] = {"ok": False, "error": str(jh)}
    else:
        healthy = 0; stale = []; failing = []; never_ran = []
        tracked = set()
        for j in jh:
            name = j["job_name"]; tracked.add(name)
            interval = intervals.get(name)
            last_ok = j["last_success_at"]
            fails = j["consecutive_failures"] or 0
            if not interval:
                if fails >= 3:
                    failing.append({"job": name, "consecutive_failures": fails,
                                    "error": j["last_error"]})
                continue
            if last_ok is None:
                stale.append({"job": name, "age_hours": None,
                              "interval_seconds": interval,
                              "consecutive_failures": fails})
                continue
            age = (now - last_ok).total_seconds()
            if age > interval * 3:
                stale.append({"job": name, "age_hours": round(age / 3600, 1),
                              "interval_seconds": interval,
                              "consecutive_failures": fails})
            else:
                healthy += 1
            if fails >= 5:
                failing.append({"job": name, "consecutive_failures": fails,
                                "error": (j["last_error"] or "")[:200]})
        missing = [n for n, _, _ in SCHEDULES if n not in tracked]
        out["sections"]["jobs"] = {
            "ok": len(stale) == 0 and len(failing) == 0,
            "defined": len(SCHEDULES),
            "tracked": len(jh),
            "healthy": healthy,
            "stale": stale,
            "failing": failing,
            "never_ran": never_ran,
            "never_tracked": missing,
        }

    # ── Alerts (last 24h) ────────────────────────────────────
    ra = await try_all("""
        SELECT module, COUNT(*) AS n, MAX(created_at) AS latest
        FROM audit_logs
        WHERE action LIKE 'alert%' AND created_at > NOW() - INTERVAL '24 hours'
        GROUP BY module ORDER BY n DESC LIMIT 10
    """)
    out["sections"]["alerts_24h"] = (
        {"ok": False, "error": str(ra)} if isinstance(ra, Exception)
        else {"ok": True, "by_module": [
            {"module": r["module"], "count": r["n"],
             "latest": r["latest"].isoformat() if r["latest"] else None}
            for r in ra
        ]}
    )

    # ── Integration cache freshness ──────────────────────────
    probes = [
        ("LibreNMS device cache",   "SELECT MAX(cached_at)     FROM network_device_cache"),
        ("Paxton ACU probe",        "SELECT MAX(last_probed)   FROM paxton_acu_status"),
        ("Lightning strikes",       "SELECT MAX(struck_at)     FROM lightning_strikes"),
        ("Chromebook cache",        "SELECT MAX(cached_at)     FROM chromebook_cache"),
        ("Room roster cache",       "SELECT MAX(cached_at)     FROM room_roster_cache"),
        ("HALO readings",           "SELECT MAX(recorded_at)   FROM halo_readings"),
        ("Wave cameras",            "SELECT MAX(last_seen)     FROM cameras"),
        ("PoE health cache",        "SELECT MAX(last_probed_at) FROM switch_poe_health"),
        ("Wireless clients",        "SELECT MAX(cached_at)     FROM wireless_client_cache"),
        ("Population snapshots",    "SELECT MAX(snapshot_at)   FROM population_snapshots"),
        ("ARP cache",               "SELECT MAX(updated_at)    FROM arp_cache"),
        ("Staff directory",         "SELECT MAX(cached_at)     FROM staff_directory"),
        ("Roster snapshots",        "SELECT MAX(updated_at)    FROM roster_snapshots"),
    ]
    integrations = []
    for label, sql in probes:
        ts = await try_scalar(sql)
        if isinstance(ts, Exception):
            integrations.append({"label": label, "ok": False,
                                 "error": str(ts)[:200]})
            continue
        if ts is None:
            integrations.append({"label": label, "ok": True, "timestamp": None,
                                 "age_seconds": None, "note": "empty"})
            continue
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds()
        integrations.append({
            "label": label, "ok": True,
            "timestamp": ts.isoformat(),
            "age_seconds": int(age),
        })
    out["sections"]["integrations"] = integrations

    # ── Audit failures (last 24h) ────────────────────────────
    fails = await try_scalar("""
        SELECT COUNT(*) FROM audit_logs
        WHERE outcome IN ('failure','error') AND created_at > NOW() - INTERVAL '24 hours'
    """)
    top = await try_all("""
        SELECT action, COUNT(*) AS n, MAX(created_at) AS latest
        FROM audit_logs
        WHERE outcome IN ('failure','error') AND created_at > NOW() - INTERVAL '24 hours'
        GROUP BY action ORDER BY n DESC LIMIT 8
    """)
    out["sections"]["audit_failures_24h"] = {
        "count": fails if not isinstance(fails, Exception) else None,
        "top_actions": [] if isinstance(top, Exception) else [
            {"action": r["action"], "count": r["n"],
             "latest": r["latest"].isoformat() if r["latest"] else None}
            for r in top
        ],
    }

    return out


def _fmt_age(sec):
    if sec is None:
        return "—"
    m = sec / 60
    if m < 60:
        return f"{m:5.1f}m"
    if m < 60 * 24:
        return f"{m/60:5.1f}h"
    return f"{m/1440:5.1f}d"


def print_report(snap: dict) -> None:
    """Human-readable CLI rendering of a collected snapshot."""
    print("\n" + "=" * 74)
    print(f"NEXUS HEALTH CHECK — {snap['generated_at']}")
    print("=" * 74)

    s = snap["sections"]

    pg = s.get("postgres", {})
    if pg.get("ok"):
        print(f"\n▸ POSTGRES  OK   db={pg['db']}  active={pg['active_connections']}")
    else:
        print(f"\n▸ POSTGRES  FAIL — {pg.get('error')}")

    r = s.get("redis", {})
    if r.get("ok"):
        print(f"▸ REDIS     OK   dbsize={r['dbsize']}  ops/sec={r['ops_per_sec']}")
    else:
        print(f"▸ REDIS     FAIL — {r.get('error')}")

    j = s.get("jobs", {})
    if not j.get("ok") and j.get("error"):
        print(f"\n▸ JOBS      FAIL — {j['error']}")
    else:
        print(
            f"\n▸ JOBS ({j.get('defined')} defined, {j.get('tracked')} tracked)  "
            f"healthy={j.get('healthy')} stale={len(j.get('stale', []))} "
            f"failing={len(j.get('failing', []))} untracked={len(j.get('never_tracked', []))}"
        )
        for st in j.get("stale", []):
            age = f"{st['age_hours']}h" if st["age_hours"] is not None else "NEVER"
            print(f"    STALE   {st['job']:<38} age={age}  interval={st['interval_seconds']//60}m  fails={st['consecutive_failures']}")
        for fl in j.get("failing", []):
            print(f"    FAIL    {fl['job']}: {fl['consecutive_failures']}x — {(fl['error'] or '')[:150]}")
        for nt in j.get("never_tracked", []):
            print(f"    UNTRK   {nt}")

    a = s.get("alerts_24h", {})
    print(f"\n▸ ALERTS (24h)")
    if a.get("by_module"):
        for m in a["by_module"]:
            print(f"    {m['module'] or '?':<20} {m['count']:>3}  latest={m['latest']}")
    else:
        print("    (none)")

    print(f"\n▸ INTEGRATION CACHE FRESHNESS")
    for p in s.get("integrations", []):
        if not p.get("ok"):
            print(f"    {p['label']:<28} ERR — {p.get('error','?')[:80]}")
        elif p.get("age_seconds") is None:
            print(f"    {p['label']:<28} (empty)")
        else:
            age_s = _fmt_age(p["age_seconds"])
            marker = "  " if p["age_seconds"] < 86400 else "* "
            print(f"    {p['label']:<28} {age_s} ago {marker}")

    af = s.get("audit_failures_24h", {})
    print(f"\n▸ AUDIT FAILURES (24h): {af.get('count')}")
    for t in af.get("top_actions", []):
        print(f"    {t['action']:<40} {t['count']:>3}  latest={t['latest']}")

    print("\n" + "=" * 74)


async def _main():
    snap = await collect()
    print_report(snap)


if __name__ == "__main__":
    asyncio.run(_main())
