"""Content-Security-Policy with per-request nonces.

Replaces the static CSP header that nginx used to set. nginx can't
generate a fresh nonce per request (it would have to do per-response
sub_filter substitution, which is expensive and error-prone), so we
build the header here on every HTML response.

Inline scripts in templates must include the nonce as an attribute:

    <script nonce="{{ request.state.csp_nonce }}">...</script>

Without the matching nonce, the browser refuses to run the script.
That's the whole point — if an attacker injects a `<script>` via an
XSS vector, they can't guess the random nonce so the injected code
is blocked.

Pentest 2026-05-17 M1 — drops `'unsafe-inline'` from script-src.
Style-src still has `'unsafe-inline'` because there are too many
inline `style=` attributes across templates to nonce them all; can
revisit later if needed.
"""
from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


# Single source of truth for the CSP header. Keep in sync with what
# the templates actually need — every external host the app loads
# scripts/styles/fonts/images from must appear here.
_BASE_CSP = (
    "default-src 'self'; "
    "script-src 'self' {nonce} https://static.cloudflareinsights.com "
    "https://maps.googleapis.com https://maps.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https://*.googleusercontent.com "
    "https://*.tile.openstreetmap.org https://server.arcgisonline.com "
    "https://maps.gstatic.com https://maps.googleapis.com "
    "https://tilecache.rainviewer.com; "
    "media-src 'self' blob:; "
    "connect-src 'self' https://static.cloudflareinsights.com "
    "https://maps.googleapis.com https://places.googleapis.com "
    "https://api.rainviewer.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class CSPNonceMiddleware(BaseHTTPMiddleware):
    """Mint a random nonce per request, expose it to templates as
    `request.state.csp_nonce`, and set the CSP header on the response
    so the browser will only run inline scripts that carry that
    nonce."""

    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        # Skip on responses that are obviously not HTML (json, static
        # files served by nginx don't reach here, but other content
        # types served from FastAPI shouldn't get a CSP they don't
        # need). Setting it on JSON is harmless but noisy.
        ct = response.headers.get("content-type", "")
        if ct.startswith("text/html"):
            response.headers["Content-Security-Policy"] = _BASE_CSP.format(
                nonce=f"'nonce-{nonce}'",
            )
        return response
