"""Staff data export registry.

Any file in this package that registers an Exporter at import time appears
as a downloadable card on `/staff/exports`. Add new files here and import
them below — the page catalog rebuilds automatically.
"""
from .base import (  # noqa: F401
    Exporter, register, register_discovery,
    ensure_discovered, get_all, get_by_id,
)
from . import swis_staff  # noqa: F401 — registers discovery on import
