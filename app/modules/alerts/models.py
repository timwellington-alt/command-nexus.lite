"""Alerts module models (lite build).

The full the district build had TechPushSubscription pointed at VoiceRecipient
via ForeignKey. That whole voice module was stripped for the lite
distribution, so this file is now a minimal placeholder — the alerts
service uses raw text() SQL against tables the initial-schema migration
creates directly (push_recipients, push_subscriptions,
alert_email_recipients, alert_module_overrides, alert_buildings).

Kept as a module-level import target because staff/router.py does an
F401 registry import on it. Empty body is fine for that purpose.
"""
