"""Shared helpers for the alerts module + /me self-service endpoints.

Both /api/alerts/recipients/{id}/routing (admin) and /api/me/recipient
(self-service) accept MAC addresses, so the normalization lives here.
"""
from __future__ import annotations


def norm_mac(mac: str | None) -> str | None:
    """Normalize a MAC address to AA:BB:CC:DD:EE:FF form.

    Returns None for empty input. Raises ValueError if the input contains
    something that isn't a 12-hex-digit MAC after stripping common separators.
    """
    if not mac:
        return None
    digits = "".join(c for c in mac.upper() if c in "0123456789ABCDEF")
    if len(digits) != 12:
        raise ValueError(f"MAC address must be 12 hex digits, got {len(digits)}")
    return ":".join(digits[i:i+2] for i in range(0, 12, 2))
