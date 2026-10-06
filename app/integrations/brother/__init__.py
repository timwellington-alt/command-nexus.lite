"""Brother label printer integrations.

Two printer families are supported, each as its own adapter:

- ``pt_raw``  — Brother P-touch (PT-E550W and similar). Speaks the raw
                LPD/P-touch raster protocol on port 515. Default backend
                for any new code. CUPS fallback lives in ``pt_adapter``.
- ``adapter`` — Brother QL (wide thermal-roll, used for staff badges).
                Speaks via the ``brother_ql`` Python library.

Multi-printer config for the P-touch family lives in the chromebook
setting ``label_printers`` as a JSON list of printer entries:

    [{"id": "main-tech", "name": "Main Tech Office",
      "host": "printer.yourdistrict.local", "tape": "12", "backend": "raw",
      "building": "PES", "is_default": true}, ...]

Use ``list_label_printers()`` to read the configured list (with legacy
single-printer fallback) and ``resolve_label_printer(...)`` to pick one
for a given print job.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class LabelPrinter:
    """A configured P-touch label printer."""
    id: str
    name: str
    host: str
    tape: str = "12"
    backend: str = "raw"
    building: str = ""
    is_default: bool = False


async def list_label_printers(db: AsyncSession) -> list[LabelPrinter]:
    """Return all configured P-touch label printers.

    Reads the JSON list from ``chromebook.label_printers``. If empty
    (i.e. the operator hasn't migrated yet), synthesizes a single
    legacy entry from ``label_printer_host`` + ``label_printer_tape``
    so existing single-printer deployments keep working unchanged.
    """
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "chromebook", "label_printers")
    items: list[LabelPrinter] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    host = (row.get("host") or "").strip()
                    if not host:
                        continue
                    items.append(LabelPrinter(
                        id=str(row.get("id") or host),
                        name=str(row.get("name") or host),
                        host=host,
                        tape=str(row.get("tape") or "12"),
                        backend=(row.get("backend") or "raw").lower(),
                        building=str(row.get("building") or ""),
                        is_default=bool(row.get("is_default")),
                    ))
        except json.JSONDecodeError:
            logger.warning("chromebook.label_printers is not valid JSON")
    if items:
        return items
    # Legacy single-printer fallback
    legacy_host = (await get_setting_value(db, "chromebook", "label_printer_host") or "").strip()
    if legacy_host:
        legacy_tape = (await get_setting_value(db, "chromebook", "label_printer_tape") or "12").strip()
        legacy_backend = (await get_setting_value(db, "chromebook", "label_printer_backend") or "raw").lower()
        return [LabelPrinter(
            id="legacy",
            name="Default",
            host=legacy_host,
            tape=legacy_tape,
            backend=legacy_backend,
            is_default=True,
        )]
    return []


async def resolve_label_printer(
    db: AsyncSession,
    *,
    printer_id: str | None = None,
    building: str | None = None,
) -> LabelPrinter | None:
    """Pick a printer for a print job. Priority:

    1. Caller-specified ``printer_id`` (explicit override).
    2. ``building`` match — printer whose ``building`` field equals
       the device's building (so PHS devices auto-route to PHS).
    3. The entry marked ``is_default``.
    4. First entry in the list.
    """
    printers = await list_label_printers(db)
    if not printers:
        return None
    if printer_id:
        for p in printers:
            if p.id == printer_id:
                return p
    if building:
        for p in printers:
            if p.building and p.building.upper() == building.upper():
                return p
    for p in printers:
        if p.is_default:
            return p
    return printers[0]
