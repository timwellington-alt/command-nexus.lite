"""
Request correlation — assigns a unique correlation ID to each request.

Carried through logs, audit records, and worker jobs for traceability.
"""

import uuid
import logging
from contextvars import ContextVar

# Context variable — available throughout the request lifecycle
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def generate_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def get_correlation_id() -> str:
    return correlation_id_var.get()


class CorrelationFilter(logging.Filter):
    """Injects correlation_id into all log records."""

    def filter(self, record):
        record.correlation_id = correlation_id_var.get("")
        return True
