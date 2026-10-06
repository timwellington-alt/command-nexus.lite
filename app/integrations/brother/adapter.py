"""
Brother QL-series label printer adapter.

Sends rendered PIL Images to a networked Brother QL printer
(default: QL-820NWBc) over raw TCP on port 9100.

Uses the brother_ql library to generate raster commands for the
printer's specific model and media type.

Printing is synchronous and returns a structured result. Every
attempt is logged via the shared external API log.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from io import BytesIO
from typing import Iterable

from app.integrations.api_log import log_api_command

logger = logging.getLogger(__name__)


@dataclass
class PrintResult:
    success: bool
    error: str | None = None
    bytes_sent: int = 0


DEFAULT_MODEL = "QL-820NWB"
DEFAULT_LABEL = "62"  # 62mm continuous (DK-2205)
DEFAULT_PORT = 9100

# brother_ql doesn't recognize every hardware SKU by its marketing name —
# the QL-820NWBc (revised hardware) uses the same raster protocol as the
# QL-820NWB, so we map marketing names to the closest supported model.
_MODEL_ALIASES = {
    "QL-820NWBC": "QL-820NWB",
    "QL820NWBC": "QL-820NWB",
    "QL-820NWB-C": "QL-820NWB",
}


def _normalize_model(model: str) -> str:
    key = (model or "").upper().replace(" ", "")
    return _MODEL_ALIASES.get(key, model or DEFAULT_MODEL)


async def print_images(
    images: Iterable,
    host: str,
    model: str = DEFAULT_MODEL,
    label: str = DEFAULT_LABEL,
    port: int = DEFAULT_PORT,
    auto_cut_between: bool = True,
) -> PrintResult:
    """
    Render one or more PIL Images into a single print job and send
    to the printer over raw TCP. Auto-cut between labels is enabled
    by default so combined jobs (badge + credentials) produce
    physically separate labels.
    """
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster
    from brother_ql.backends.helpers import send  # not used — we open our own socket

    if not host:
        log_api_command(
            system="brother_ql",
            action="print",
            target=host or "",
            response_status="error",
            response_summary="printer host not configured",
        )
        return PrintResult(success=False, error="Printer host not configured")

    image_list = [img for img in images if img is not None]
    if not image_list:
        log_api_command(
            system="brother_ql",
            action="print",
            target=host,
            payload=f"model={model} label={label}",
            response_status="error",
            response_summary="no images to print",
        )
        return PrintResult(success=False, error="No images to print")

    resolved_model = _normalize_model(model)

    def _build_raster() -> bytes:
        qlr = BrotherQLRaster(resolved_model)
        qlr.exception_on_warning = True
        # brother_ql's `threshold` is a PERCENTAGE (0-100), NOT a raw
        # 0-255 pixel value. The library does:
        #     raw = int((100 - threshold) / 100 * 255)
        # then quantizes using `pixel < raw -> 0 else 255`. Passing a
        # raw pixel value like 128 clamps to 0 and prints the entire
        # label solid black. 70 is the library default and works
        # correctly for both crisp text and the pre-dithered photo
        # (whose pixels are already pure 0/255).
        convert(
            qlr=qlr,
            images=image_list,
            label=label,
            rotate="0",
            threshold=70.0,
            dither=False,
            compress=False,
            red=False,
            dpi_600=False,
            hq=True,
            cut=auto_cut_between,
        )
        return qlr.data

    try:
        raster = await asyncio.to_thread(_build_raster)
    except Exception as e:
        # Some brother_ql exceptions have empty str() — use repr() so the
        # class name (e.g. BrotherQLUnknownModel) always makes it to the log.
        err = str(e) or type(e).__name__
        logger.exception("brother_ql raster build failed (model=%s)", resolved_model)
        log_api_command(
            system="brother_ql",
            action="print",
            target=f"{host}:{port}",
            payload=f"model={resolved_model} label={label} images={len(image_list)}",
            response_status="error",
            response_summary=f"raster build failed: {err[:150]}",
        )
        return PrintResult(success=False, error=f"raster build failed: {err[:150]}")

    def _send_tcp(data: bytes) -> int:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(data)
            # Drain any status bytes the printer returns without blocking long
            sock.settimeout(1.5)
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
            except socket.timeout:
                pass
        return len(data)

    try:
        sent = await asyncio.to_thread(_send_tcp, raster)
    except Exception as e:
        logger.warning(f"Brother print TCP send failed to {host}:{port} — {e}")
        log_api_command(
            system="brother_ql",
            action="print",
            target=f"{host}:{port}",
            payload=f"model={resolved_model} label={label} images={len(image_list)} bytes={len(raster)}",
            response_status="error",
            response_summary=f"printer unreachable: {str(e)[:150]}",
        )
        return PrintResult(success=False, error=f"printer unreachable: {str(e)[:150]}")

    log_api_command(
        system="brother_ql",
        action="print",
        target=f"{host}:{port}",
        payload=f"model={resolved_model} label={label} images={len(image_list)} bytes={sent}",
        response_status="ok",
        response_summary=f"sent {sent} bytes",
    )
    return PrintResult(success=True, bytes_sent=sent)
