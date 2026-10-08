"""Student email format resolver.

Nexus-lite exposes two settings that control how student district
emails are generated:

    roster.student_email_format         (preset name)
    roster.student_email_collision      (preset name)

The hardcoded ``build_expected_email()`` in ``clever_service.py``
delegates here. All callers pass through this module.

Keeping format + collision as PRESET IDS instead of a free-text
template keeps the Settings UI readable and lets us ship sane
compliance-check regexes keyed by preset.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ─── Format presets ───────────────────────────────────────────────────
# Each preset defines:
#   label     — human-facing name for the dropdown
#   build     — (last, first, grad_year, sid) -> local-part
#   is_match  — (local_part, last, first, grad_year) -> bool, used by
#               check_email_compliance to detect compliant vs. drifted
#               emails without hardcoding the regex.

def _clean(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").strip().lower())


def _yy(grad_year: int) -> str:
    return str(grad_year)[-2:]


FORMATS = {
    "last_first_init_yy": {
        "label": "lastname + first initial + 2-digit grad year  (smithj26)",
        "build": lambda last, first, year, sid: f"{_clean(last)}{(_clean(first) or 'x')[0]}{_yy(year)}",
        # year-tolerant (+/- 2) — accommodates grade retention.
        "regex": lambda last, first, year: re.compile(
            rf"^{_clean(last)}{(_clean(first) or 'x')[0]}(?:{int(_yy(year))-2:02d}|{int(_yy(year))-1:02d}|{_yy(year)}|{(int(_yy(year))+1)%100:02d}|{(int(_yy(year))+2)%100:02d})\d*$"
        ),
    },
    "last_first2_yy": {
        "label": "lastname + 2 first-name letters + 2-digit grad year  (smithjo26)",
        "build": lambda last, first, year, sid: f"{_clean(last)}{((_clean(first) or 'xx') + 'x')[:2]}{_yy(year)}",
        "regex": lambda last, first, year: re.compile(
            rf"^{_clean(last)}{((_clean(first) or 'xx') + 'x')[:2]}(?:{int(_yy(year))-2:02d}|{int(_yy(year))-1:02d}|{_yy(year)}|{(int(_yy(year))+1)%100:02d}|{(int(_yy(year))+2)%100:02d})\d*$"
        ),
    },
    "first_dot_last_yy": {
        "label": "firstname.lastname + 2-digit grad year  (john.smith26)",
        "build": lambda last, first, year, sid: f"{_clean(first) or 'x'}.{_clean(last)}{_yy(year)}",
        "regex": lambda last, first, year: re.compile(
            rf"^{_clean(first) or 'x'}\.{_clean(last)}(?:{int(_yy(year))-2:02d}|{int(_yy(year))-1:02d}|{_yy(year)}|{(int(_yy(year))+1)%100:02d}|{(int(_yy(year))+2)%100:02d})\d*$"
        ),
    },
    "first_dot_last": {
        "label": "firstname.lastname (no year)  (john.smith)",
        "build": lambda last, first, year, sid: f"{_clean(first) or 'x'}.{_clean(last)}",
        "regex": lambda last, first, year: re.compile(
            rf"^{_clean(first) or 'x'}\.{_clean(last)}\d*$"
        ),
    },
    "sid": {
        "label": "SIS student ID  (123456)",
        "build": lambda last, first, year, sid: str(sid or "").strip() or "x",
        "regex": lambda last, first, year: re.compile(r"^\d+$"),
    },
}

DEFAULT_FORMAT = "last_first_init_yy"


# ─── Collision strategies ─────────────────────────────────────────────
# Called when the primary email already exists in Google and the SID
# of the existing account doesn't match this student. `attempt` is a
# zero-based retry counter — strategy decides what to try next.

COLLISIONS = {
    "extend_first_name": {
        "label": "Add more first-name letters  (smithj26 → smithjo26 → smithjoh26)",
    },
    "numeric_suffix": {
        "label": "Append a number  (smithj26 → smithj262 → smithj263)",
    },
    "reject_for_review": {
        "label": "Reject and queue for admin review",
    },
}

DEFAULT_COLLISION = "extend_first_name"


@dataclass
class EmailBuilder:
    """Pass this into provisioning / compliance code instead of raw strings."""
    format_key: str = DEFAULT_FORMAT
    collision_key: str = DEFAULT_COLLISION
    domain: str = ""

    @classmethod
    async def load(cls, db) -> "EmailBuilder":
        from app.modules.settings.repository import get_setting_value
        fmt = (await get_setting_value(db, "roster", "student_email_format") or DEFAULT_FORMAT).strip()
        if fmt not in FORMATS:
            fmt = DEFAULT_FORMAT
        col = (await get_setting_value(db, "roster", "student_email_collision") or DEFAULT_COLLISION).strip()
        if col not in COLLISIONS:
            col = DEFAULT_COLLISION
        domain = (await get_setting_value(db, "roster", "student_email_domain") or "").strip()
        return cls(format_key=fmt, collision_key=col, domain=domain)

    def build_local(self, last: str, first: str, grad_year: int,
                    *, sid: str | None = None, attempt: int = 0) -> str | None:
        """Return the local-part of the email for this (student, attempt)
        pair, or None if the collision strategy rejects at this attempt."""
        base = FORMATS[self.format_key]["build"](last, first, grad_year, sid)
        if attempt == 0:
            return base

        if self.collision_key == "numeric_suffix":
            # Primary variant used attempt==0, so retries append 2, 3, ...
            return f"{base}{attempt + 1}"

        if self.collision_key == "extend_first_name":
            # Only meaningful for formats that embed first name. For sid /
            # first_dot_last_no_year, fall through to numeric suffix as a
            # safety net.
            cf = _clean(first) or "x"
            if self.format_key == "last_first_init_yy" and len(cf) > 1 + attempt:
                return f"{_clean(last)}{cf[:1 + attempt + 1]}{_yy(grad_year)}"
            if self.format_key == "last_first2_yy" and len(cf) > 2 + attempt:
                return f"{_clean(last)}{cf[:2 + attempt + 1]}{_yy(grad_year)}"
            if self.format_key == "first_dot_last_yy" and len(cf) > 1 + attempt:
                return f"{cf[:1 + attempt + 1]}{_clean(last)}{_yy(grad_year)}"
            # Ran out of first-name letters OR format doesn't use first
            # name — fall back to numeric suffix.
            return f"{base}{attempt + 1}"

        # reject_for_review: no retries, caller must queue
        return None

    def build(self, last: str, first: str, grad_year: int,
              *, sid: str | None = None, attempt: int = 0) -> str | None:
        local = self.build_local(last, first, grad_year, sid=sid, attempt=attempt)
        if local is None or not self.domain:
            return local
        return f"{local}@{self.domain}"

    def is_compliant(self, actual_email: str, last: str, first: str,
                     grad_year: int) -> bool:
        """Return True if `actual_email` matches the configured format
        for this student (year-tolerant for retained students)."""
        if "@" in actual_email:
            local, _, dom = actual_email.partition("@")
            if self.domain and dom.lower() != self.domain.lower():
                return False
        else:
            local = actual_email
        rx = FORMATS[self.format_key]["regex"](last, first, grad_year)
        return bool(rx.match(local.lower()))
