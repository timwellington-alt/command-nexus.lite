"""
Brother P-touch label printer adapter.

Prints PIL Images to a networked PT-E550W via CUPS using the
printer-driver-ptouch package. CUPS handles the raster protocol
through the LPR backend (port 515).

Prerequisites (host, not container):
  apt install cups printer-driver-ptouch
  lpadmin -p PT-E550W -v lpd://PRINTER_IP/binary_p1 \
    -m "ptouch:0/ppd/ptouch-driver/Brother-PT-550A-ptouch-pt.ppd" -E
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Iterable

from PIL import Image

from app.integrations.api_log import log_api_command

logger = logging.getLogger(__name__)

DPI = 180
CUPS_PRINTER = "PT-E550W"

# Tape width → pixel height at 180 dpi
TAPE_WIDTHS = {
    "6":  32,
    "9":  52,
    "12": 76,
    "18": 112,
    "24": 128,
}


@dataclass
class PrintResult:
    success: bool
    error: str | None = None
    bytes_sent: int = 0


async def print_labels(
    images: Iterable[Image.Image],
    host: str,  # kept for API compat — CUPS config has the address
    tape_width: str = "24",
    **_kwargs,
) -> PrintResult:
    """Print one or more PIL Images to the PT-E550W via CUPS."""
    image_list = [img for img in images if img is not None]
    if not image_list:
        log_api_command(
            system="brother_pt", action="print", target=CUPS_PRINTER,
            payload=f"tape={tape_width}mm",
            response_status="error", response_summary="no images to print",
        )
        return PrintResult(success=False, error="No images to print")

    tape_px = TAPE_WIDTHS.get(tape_width, 76)
    tape_mm = int(tape_width)
    total_bytes = 0
    errors = []

    def _print_one(img: Image.Image) -> int:
        # Ensure image height matches tape
        if img.height != tape_px:
            ratio = tape_px / img.height
            new_w = max(1, int(img.width * ratio))
            img = img.resize((new_w, tape_px), Image.LANCZOS)

        # Convert to 1-bit for crisp output
        bw = img.convert("1")

        # Compute label length in mm from image width at 180 DPI
        label_mm = max(10, int(bw.width * 25.4 / DPI) + 2)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as f:
            bw.save(f.name, format="PNG", dpi=(DPI, DPI))
            size = f.seek(0, 2)

            # Use custom page size: width=tape_mm, height=label_mm
            # This prevents the driver from padding with blank lines
            page_size = f"Custom.{tape_mm}x{label_mm}mm"

            result = subprocess.run(
                ["lp", "-d", CUPS_PRINTER,
                 "-o", f"PageSize={page_size}",
                 "-o", "ExtraMargin=0mm",
                 f.name],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"lp exit {result.returncode}")
            logger.info(f"CUPS job submitted: {result.stdout.strip()}")
            return size

    try:
        for img in image_list:
            sent = await asyncio.to_thread(_print_one, img)
            total_bytes += sent
    except Exception as e:
        err = str(e)[:150]
        logger.warning(f"PT-E550W CUPS print failed: {err}")
        log_api_command(
            system="brother_pt", action="print", target=CUPS_PRINTER,
            payload=f"tape={tape_width}mm images={len(image_list)}",
            response_status="error", response_summary=f"print failed: {err}",
        )
        return PrintResult(success=False, error=f"print failed: {err}")

    log_api_command(
        system="brother_pt", action="print", target=CUPS_PRINTER,
        payload=f"tape={tape_width}mm images={len(image_list)} bytes={total_bytes}",
        response_status="ok",
        response_summary=f"printed {len(image_list)} label(s)",
    )
    return PrintResult(success=True, bytes_sent=total_bytes)
