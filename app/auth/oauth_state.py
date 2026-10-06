"""
OAuth state management — stored in Redis with 5-minute TTL.

Not stored in session cookies. Prevents state from surviving
beyond the login flow window.
"""

import secrets
import logging

import redis.asyncio as aioredis
from app.config import get_settings

logger = logging.getLogger(__name__)

STATE_TTL = 300  # 5 minutes


async def _get_redis():
    settings = get_settings()
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def create_oauth_state() -> str:
    """Generate a state token and store it in Redis with 5-min TTL."""
    state = secrets.token_urlsafe(32)
    r = await _get_redis()
    try:
        await r.setex(f"oauth_state:{state}", STATE_TTL, "1")
    finally:
        await r.aclose()
    return state


async def validate_oauth_state(state: str) -> bool:
    """Validate and consume a state token. Returns True if valid."""
    if not state:
        return False
    r = await _get_redis()
    try:
        result = await r.getdel(f"oauth_state:{state}")
    finally:
        await r.aclose()
    return result is not None
