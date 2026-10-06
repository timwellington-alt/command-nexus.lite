"""Roster (student) data export registry.

Any file in this package that registers an Exporter at import time appears
as a downloadable card on `/roster/exports`. Add new files here and import
them below.
"""
from .base import (  # noqa: F401
    Exporter, register, register_discovery,
    ensure_discovered, get_all, get_by_id,
)
from . import swis_students  # noqa: F401 — registers on import
