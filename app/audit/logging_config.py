"""
Structured logging configuration.

All log output includes correlation_id for request traceability.
Uses JSON format in production, human-readable in development.
"""

import logging
import json
import sys
from datetime import datetime, timezone

from app.audit.correlation import CorrelationFilter
from app.config import get_settings


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for production."""

    def format(self, record):
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", ""),
            "module": record.module,
            "function": record.funcName,
        })


class ReadableFormatter(logging.Formatter):
    """Human-readable formatter for development."""

    def format(self, record):
        cid = getattr(record, "correlation_id", "")
        cid_str = f" [{cid}]" if cid else ""
        return f"{record.levelname:5s}{cid_str} {record.name}: {record.getMessage()}"


def configure_logging():
    """Set up structured logging with correlation ID injection."""
    settings = get_settings()

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Remove existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(CorrelationFilter())

    if settings.is_production:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(ReadableFormatter())

    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
