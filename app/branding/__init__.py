"""Process-wide branding cache.

Exposes `get()` and `invalidate()` so the settings router can force a refresh
after a save. The middleware populates `request.state.branding` from this
cache so templates can SSR the branded logo without the async fetch flicker.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


DEFAULT_TITLE = "NEXUS"
DEFAULT_COLOR = "#58a6ff"
TTL_SECONDS = 300  # 5-minute refresh window; invalidate() zeros this


@dataclass(frozen=True)
class Branding:
    district_name: str        # raw value from settings, empty string if unset
    brand_color: str          # hex, falls back to accent blue
    title: str                # what goes in <span id="brand-title">
    page_title_prefix: str    # what replaces "Command Nexus" in <title>


_cache: Branding | None = None
_cache_loaded_at: float = 0.0
_refresh_lock = asyncio.Lock()


def _build(district_name: str | None, brand_color: str | None) -> Branding:
    dn = (district_name or "").strip()
    color = (brand_color or "").strip() or DEFAULT_COLOR
    if dn:
        title = f"{dn.upper()} NEXUS"
        page_prefix = f"{dn} Nexus"
    else:
        title = DEFAULT_TITLE
        page_prefix = "Command Nexus"
    return Branding(district_name=dn, brand_color=color, title=title, page_title_prefix=page_prefix)


DEFAULT = _build(None, None)


async def refresh(db: AsyncSession) -> Branding:
    """Force-reload from Settings. Updates the module cache."""
    global _cache, _cache_loaded_at
    from app.modules.settings.repository import get_setting_value
    dn = await get_setting_value(db, "branding", "district_name")
    color = await get_setting_value(db, "branding", "brand_color")
    _cache = _build(dn, color)
    _cache_loaded_at = time.monotonic()
    return _cache


async def get(db: AsyncSession) -> Branding:
    """Return cached branding. Refresh if stale or uninitialized.
    Background-only coroutine safe: lock prevents thundering-herd."""
    global _cache
    if _cache is not None and (time.monotonic() - _cache_loaded_at) < TTL_SECONDS:
        return _cache
    async with _refresh_lock:
        # Double-check after acquiring the lock — another caller may have just refreshed
        if _cache is not None and (time.monotonic() - _cache_loaded_at) < TTL_SECONDS:
            return _cache
        try:
            return await refresh(db)
        except Exception:
            # Don't let branding load failures break page rendering — fall back
            if _cache is not None:
                return _cache
            return DEFAULT


def cached_or_default() -> Branding:
    """Sync accessor for middleware: returns whatever's in the cache, or the
    defaults if nothing's been loaded yet. Never blocks."""
    return _cache if _cache is not None else DEFAULT


def invalidate() -> None:
    """Drop the cache. Next `get()` call will refresh from DB."""
    global _cache, _cache_loaded_at
    _cache = None
    _cache_loaded_at = 0.0
