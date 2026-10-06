"""Shared top-nav module visibility helper.

Every page-rendering router needs to pass a `modules` dict to the
template context so `_mobile_nav.html` knows which top-nav entries
to show for the current user. Before this helper existed, each
router defined its own `_modules_dict` / `_modules_context` — and
they drifted (carehawk missing `chat`+`tickets`, inventory missing
`chat`+`tickets`, others adding new entries one at a time).

Centralize here. Callers do:

    from app.nav import modules_for_user
    ...
    "modules": modules_for_user(perms),

Adding a new top-nav module = one line here + a new entry in
`_mobile_nav.html`.
"""
from __future__ import annotations

from app.policies.engine import check_permission


# (module_key, required_action). Keep alphabetized by module_key
# below — when adding entries, add them in order so diffs stay clean.
# `facilities` is the odd one because two perms grant access.
_NAV_ENTRIES: list[tuple[str, str]] = [
    ("access",         "access.view"),
    ("audit",          "settings.manage"),
    ("chat",           "chat.admin"),
    ("chromebook",     "chromebook.view"),
    ("docs",           "docs.view"),
    ("inventory",      "inventory.view"),
    ("me",             "alerts.tech.view"),
    ("network",        "network.view"),
    ("operations",     "operations.view"),
    ("phones",         "network.view"),
    ("roster",         "roster.view"),
    ("security",       "security.view"),
    ("settings",       "settings.manage"),
    ("staff",          "staff.view"),
    ("tickets",        "tickets.view"),
    ("transportation", "transportation.view"),
]


def modules_for_user(permissions: list[dict]) -> dict[str, bool]:
    """Return `{module_key: bool}` for every top-nav entry.

    Used as `modules` in every page template context."""
    out = {k: check_permission(permissions, action) for k, action in _NAV_ENTRIES}
    # facilities is a union — operators with EITHER carehawk view OR
    # inventory view see the facilities entry (CareHawk lives there,
    # so does the floor-plan editor).
    out["facilities"] = (
        check_permission(permissions, "carehawk.view")
        or check_permission(permissions, "inventory.view")
    )
    return out
