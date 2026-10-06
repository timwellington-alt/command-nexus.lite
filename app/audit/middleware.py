"""
Correlation ID middleware — sets a unique ID for each request.

Available via get_correlation_id() throughout the request lifecycle.
Passed through to audit records and structured logs.
"""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.audit.correlation import correlation_id_var, generate_correlation_id


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Assigns a correlation ID to each request."""

    async def dispatch(self, request: Request, call_next):
        cid = generate_correlation_id()
        token = correlation_id_var.set(cid)
        request.state.correlation_id = cid

        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = cid
            return response
        finally:
            correlation_id_var.reset(token)
