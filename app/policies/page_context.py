"""
Shared page-context helpers so every route handler doesn't repeat the
same 15-key permission dict for the top nav.

`build_page_modules(perms)` returns the dict `_top_nav.html` and every
page's user-area logic expects. Adding a new module → one line here,
not 20 route edits.
"""

from __future__ import annotations

from app.policies.engine import check_permission


def build_page_modules(permissions: list) -> dict[str, bool]:
    """
    Compute the `modules` context dict for a page render.

    Keys mirror the top-nav conditionals in `_top_nav.html` plus a few
    used by individual pages (`me` for the profile link, `docs` /
    `operations` as historical aliases, `phones` for the phones-under-
    network nav card, `chromebook` for the same).

    Every route handler that renders a template with `_top_nav.html` in
    it should:

        from app.policies.page_context import build_page_modules
        ...
        modules = build_page_modules(perms)

    instead of hand-writing the dict.
    """
    return {
        "audit":          check_permission(permissions, "settings.manage"),
        "me":             check_permission(permissions, "alerts.tech.view"),
        "settings":       check_permission(permissions, "settings.manage"),
        "staff":          check_permission(permissions, "staff.view"),
        "access":         check_permission(permissions, "access.view"),
        "roster":         check_permission(permissions, "roster.view"),
        "roster_analytics": check_permission(permissions, "roster.analytics.view"),
        "network":        check_permission(permissions, "network.view"),
        "transportation": check_permission(permissions, "transportation.view"),
        "chromebook":     check_permission(permissions, "chromebook.view"),
        "docs":           check_permission(permissions, "docs.view"),
        "operations":     check_permission(permissions, "operations.view"),
        "phones":         check_permission(permissions, "network.view"),
        "security":       check_permission(permissions, "security.view"),
        "inventory":      check_permission(permissions, "inventory.view"),
        "facilities":     check_permission(permissions, "carehawk.view"),
        "chat":           check_permission(permissions, "chat.admin"),
        "tickets":        check_permission(permissions, "tickets.view"),
    }
