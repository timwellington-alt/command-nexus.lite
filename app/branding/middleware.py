"""Branding middleware — puts a Branding snapshot on every request.

Templates read it as `request.state.branding.title` / `.brand_color` /
`.page_title_prefix` to render the logo server-side and avoid the
"NEXUS → TROJAN NEXUS" flash on first paint.

The middleware itself is sync-fast — it reads from a module-level cache.
Background refresh happens via `app.branding.get(db)` which the startup
hook + settings-save hook call. The first request after startup gets the
default values if the cache hasn't loaded yet, but that's a non-issue
because we pre-load the cache in `lifespan`.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app import branding


class BrandingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.branding = branding.cached_or_default()
        return await call_next(request)
