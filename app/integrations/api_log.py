"""
External API command logger — DEPRECATED.

The CSV-based api_commands.csv logging has been retired. All external
system writes are now audited through the database audit_logs table
via log_action() in app/audit/service.py.

These functions are kept as no-ops so existing callers don't break.
"""

import functools


def log_api_command(
    system: str = "",
    action: str = "",
    target: str = "",
    payload: str = "",
    response_status: str = "",
    response_summary: str = "",
    caller: str = "",
) -> None:
    """No-op — CSV logging retired. Use log_action() for audit trail."""
    pass


def logged_write(system: str):
    """No-op decorator — CSV logging retired."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await fn(*args, **kwargs)
        return wrapper
    return decorator
