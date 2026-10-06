"""
Redis-backed session store.

Sessions are stored in Redis, not in cookies or in-memory.
Cookie contains a signed session ID (HMAC-SHA256). All session data
lives in Redis and can be invalidated server-side.

Session ID is rotated on login to prevent session fixation.
"""

import hashlib
import hmac
import json
import uuid
import logging
from datetime import timedelta

import redis.asyncio as aioredis
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(days=14)
SESSION_COOKIE = "nexus_session"


def _sign_session_id(session_id: str, secret: str) -> str:
    """Sign a session ID for cookie storage. Returns 'session_id.signature'."""
    sig = hmac.new(
        secret.encode(),
        session_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{session_id}.{sig}"


def _unsign_session_id(signed_value: str, secret: str) -> str | None:
    """Verify and unwrap a signed session cookie. Returns session_id or None if invalid."""
    if not signed_value or "." not in signed_value:
        return None
    session_id, sig = signed_value.rsplit(".", 1)
    expected = hmac.new(
        secret.encode(),
        session_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    return session_id


async def rotate_session(
    redis_client,
    old_signed_id: str | None,
    new_data: dict,
    secret_key: str,
    max_age: int,
) -> str:
    """
    Issue a new session ID, persist new_data under it, and delete the old session.
    Returns the new signed cookie value.

    Called on login to prevent session fixation — the pre-auth session ID
    is never reused for an authenticated session.
    """
    # Delete old session if it existed
    if old_signed_id:
        old_id = _unsign_session_id(old_signed_id, secret_key)
        if old_id:
            await redis_client.delete(f"session:{old_id}")

    # Generate new session ID
    new_id = uuid.uuid4().hex
    await redis_client.setex(
        f"session:{new_id}",
        max_age,
        json.dumps(new_data),
    )
    return _sign_session_id(new_id, secret_key)


class RedisSessionMiddleware:
    """
    ASGI middleware that stores session data in Redis.

    Cookie contains a signed session ID (HMAC-SHA256). Tampering results
    in an empty session, not an error. All session data lives in Redis.
    """

    def __init__(self, app: ASGIApp):
        self.app = app
        settings = get_settings()
        self.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        self.secret_key = settings.app_secret_key
        self.max_age = int(SESSION_TTL.total_seconds())
        self.is_production = settings.is_production

    def _is_secure_request(self, scope: Scope) -> bool:
        """Check if the original request was over HTTPS (via X-Forwarded-Proto or scheme)."""
        if scope.get("scheme") == "https":
            return True
        headers = dict(scope.get("headers", []))
        proto = headers.get(b"x-forwarded-proto", b"").decode()
        return proto == "https"

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)

        # Unsign the cookie — tampered or unsigned cookies yield None
        raw_cookie = connection.cookies.get(SESSION_COOKIE)
        session_id = _unsign_session_id(raw_cookie, self.secret_key) if raw_cookie else None

        scope["session"] = {}

        # Load existing session from Redis
        if session_id:
            try:
                data = await self.redis.get(f"session:{session_id}")
                if data:
                    scope["session"] = json.loads(data)
            except Exception as e:
                logger.warning(f"Session load error: {e}")

        # Store reference to Redis client and settings for rotate_session
        scope["_session_redis"] = self.redis
        scope["_session_secret"] = self.secret_key
        scope["_session_max_age"] = self.max_age
        scope["_session_cookie_raw"] = raw_cookie
        scope["_session_is_production"] = self.is_production

        # Capture the original session to detect changes
        original_session = dict(scope.get("session", {}))

        secure = self._is_secure_request(scope)

        async def send_wrapper(message: Message):
            if message["type"] == "http.response.start":
                session = scope.get("session", {})

                # Check if session was rotated (login flow sets this)
                new_signed_cookie = scope.get("_session_new_cookie")
                if new_signed_cookie:
                    cookie = (
                        f"{SESSION_COOKIE}={new_signed_cookie}; Path=/; "
                        f"Max-Age={self.max_age}; HttpOnly; SameSite=Lax"
                    )
                    if secure:
                        cookie += "; Secure"
                    existing = message.get("headers", [])
                    message["headers"] = list(existing) + [
                        (b"set-cookie", cookie.encode())
                    ]

                # Session was cleared (logout)
                elif not session and session_id:
                    try:
                        await self.redis.delete(f"session:{session_id}")
                    except Exception:
                        pass
                    cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                    if secure:
                        cookie += "; Secure"
                    existing = message.get("headers", [])
                    message["headers"] = list(existing) + [
                        (b"set-cookie", cookie.encode())
                    ]

                # Session changed but wasn't rotated (normal updates)
                elif session and session != original_session and not new_signed_cookie:
                    sid = session_id or uuid.uuid4().hex
                    try:
                        await self.redis.setex(
                            f"session:{sid}",
                            self.max_age,
                            json.dumps(session),
                        )
                    except Exception as e:
                        logger.error(f"Session save error: {e}")

                    if not session_id:
                        signed_sid = _sign_session_id(sid, self.secret_key)
                        cookie = (
                            f"{SESSION_COOKIE}={signed_sid}; Path=/; "
                            f"Max-Age={self.max_age}; HttpOnly; SameSite=Lax"
                        )
                        if secure:
                            cookie += "; Secure"
                        existing = message.get("headers", [])
                        message["headers"] = list(existing) + [
                            (b"set-cookie", cookie.encode())
                        ]

            await send(message)

        await self.app(scope, receive, send_wrapper)
