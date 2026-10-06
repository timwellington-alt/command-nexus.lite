"""Alerts service (lite).

Minimal notification dispatcher for the shareable distribution. The
full the district build routes alerts across push (web push), voice (baresip
IVR to phones), and cast (chromecast overlay). The lite build ships
only two channels:
  1. Web push (Chrome/Edge/Safari, PWA-capable)
  2. Gmail email via the service account impersonation

Both channels are best-effort; audit_logs is the durable record. The
`dispatch_routed_alert` signature is unchanged so callers (worker
watchdog, staff sync alerts, attendance spike, etc.) work as-is.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def _load_module_override(db: AsyncSession, source_module: str) -> dict:
    """Per-source-module override — allows disabling channels or setting
    a per-module quiet-hour bypass. Same schema as the full build so
    the alert_module_overrides table doesn't need a migration change."""
    row = (await db.execute(text("""
        SELECT push_enabled, email_enabled, bypass_quiet_hours
        FROM alert_module_overrides WHERE module = :m
    """).bindparams(m=source_module))).mappings().first()
    if row is None:
        return {"push_enabled": True, "email_enabled": True, "bypass_quiet_hours": False}
    return dict(row)


async def _load_vapid_config(db: AsyncSession) -> dict | None:
    from app.modules.settings.repository import get_setting_value
    priv = await get_setting_value(db, "push", "vapid_private_key")
    pub = await get_setting_value(db, "push", "vapid_public_key")
    sub = await get_setting_value(db, "push", "vapid_subject") or "mailto:admin@localhost"
    if not (priv and pub):
        return None
    return {"private_key": priv, "public_key": pub, "subject": sub}


async def send_push_to_recipient(
    db: AsyncSession, recipient_id: int, title: str, body: str,
) -> int:
    """Deliver a Web Push notification to every subscription tied to
    a push_recipients.id. Returns the count of successful pushes."""
    cfg = await _load_vapid_config(db)
    if cfg is None:
        return 0
    rows = (await db.execute(text("""
        SELECT endpoint, p256dh, auth_key
        FROM push_subscriptions
        WHERE recipient_id = :r
    """).bindparams(r=recipient_id))).mappings().all()
    if not rows:
        return 0

    from pywebpush import webpush, WebPushException
    payload = json.dumps({"title": title, "body": body})
    ok = 0
    for r in rows:
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info={
                    "endpoint": r["endpoint"],
                    "keys": {"p256dh": r["p256dh"], "auth": r["auth_key"]},
                },
                data=payload,
                vapid_private_key=cfg["private_key"],
                vapid_claims={"sub": cfg["subject"]},
            )
            ok += 1
        except WebPushException as e:
            logger.warning("push failed to recipient %s: %s", recipient_id, e)
    return ok


async def send_push_to_all_active(
    db: AsyncSession, title: str, body: str,
) -> dict:
    """Fan out to every active push recipient — used for "all-hands"
    notifications like a stale-worker page."""
    rows = (await db.execute(text(
        "SELECT id, label FROM push_recipients WHERE active = TRUE"
    ))).mappings().all()
    out = {}
    for r in rows:
        out[r["label"]] = await send_push_to_recipient(db, r["id"], title, body)
    return out


async def _send_email_alert(
    db: AsyncSession, severity: str, summary: str, source_module: str,
) -> int:
    """Send an email digest to every entry in alert_email_recipients.
    Uses Gmail send via the service-account impersonation configured
    in Settings → Google."""
    rows = (await db.execute(text(
        "SELECT email FROM alert_email_recipients WHERE active = TRUE"
    ))).mappings().all()
    if not rows:
        return 0
    try:
        from app.integrations.gmail.adapter import send_email
    except Exception:
        return 0
    subject = f"[Nexus {severity.upper()}] {source_module}"
    body = f"{summary}\n\n(source: {source_module})"
    ok = 0
    for r in rows:
        try:
            await send_email(db, to=r["email"], subject=subject, body=body)
            ok += 1
        except Exception as e:
            logger.warning("email alert failed to %s: %s", r["email"], e)
    return ok


async def dispatch_routed_alert(
    db: AsyncSession,
    *,
    building_code: str,
    severity: str,
    source_module: str,
    source_ref: str,
    summary: str,
    affected_count: int = 1,
) -> dict:
    """Route an alert through push + email. Always writes to audit_logs
    regardless of channel outcome — audit is the durable record.

    Returns a summary dict with per-channel counts."""
    override = await _load_module_override(db, source_module)
    outcome = {"push": {}, "email": 0, "audit_written": False}

    if override.get("push_enabled", True):
        outcome["push"] = await send_push_to_all_active(
            db,
            title=f"Nexus Alert — {severity.upper()}",
            body=f"{building_code}: {summary}" if building_code and building_code != "UNKNOWN" else summary,
        )

    if override.get("email_enabled", True):
        outcome["email"] = await _send_email_alert(db, severity, summary, source_module)

    try:
        await db.execute(text("""
            INSERT INTO audit_logs (actor, action, module, target, details, created_at)
            VALUES (:a, 'alerts.dispatched', :m, :t,
                    CAST(:d AS JSONB), NOW())
        """).bindparams(
            a="system:alerts",
            m=source_module,
            t=source_ref,
            d=json.dumps({
                "severity": severity,
                "building_code": building_code,
                "summary": summary[:400],
                "affected_count": affected_count,
                "outcome": outcome,
            }),
        ))
        outcome["audit_written"] = True
    except Exception as e:
        logger.exception("alerts audit write failed: %s", e)

    return outcome
