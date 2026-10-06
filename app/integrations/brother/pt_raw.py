"""Brother P-touch raw protocol adapter (PT-E550W).

Speaks the P-touch raster command set directly over LPD (port 515),
queue ``BINARY_P1``. Replaces the CUPS+PPD bridge in ``pt_adapter.py``
which spawned excess tape and silently dropped status info.

Protocol reverse-engineered from a wireshark capture of the official
Brother P-touch Editor printing to the PT-E550W on 2026-05-11 (capture
at ``Project Specs/BT550W/BT550W.pcapng``). See the in-line docstring
of ``build_data_file`` for the byte-by-byte breakdown.

Knobs surfaced via kwargs to ``print_labels``:
  cut_at_end    — leave one cut at the end of the chain (default True)
  half_cut      — cut only the laminate, leave the backing (default True)
  auto_cut      — cut between each label of a multi-label job (default False)
  margin_dots   — leading tape margin in dots at 180 DPI (default 14)
  compression   — 'packbits' | 'none' (default 'packbits' — matches the
                  official Brother software and shrinks raster ~50%)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Iterable

from PIL import Image

from app.integrations.api_log import log_api_command

logger = logging.getLogger(__name__)

DPI = 180
LPD_PORT = 515
LPD_QUEUE = b"BINARY_P1"
PT_HOST_NAME = b"nexus"   # appears in LPD control file as H/P
PT_USER_NAME = b"nexus"

# Printable dots per tape width on the PT-E550W's 128-pin head. The head
# is always 128 dots; only the centered window matching the loaded tape
# actually prints. ``offset`` is the left-pad in dots needed to center
# the printable band inside the 128-bit raster line.
#                                              dots  offset_in_128
TAPE_DOT_TABLE: dict[str, tuple[int, int]] = {
    "3.5": (24, 52),
    "6":   (32, 48),
    "9":   (50, 39),
    "12":  (70, 29),
    "18":  (112, 8),
    "24":  (128, 0),
    "36":  (192, 0),   # PT-9000-series; the E550W maxes at 24mm tape
}

# Bytes per raster line (head width) — always 16 for 128-dot heads.
HEAD_BYTES = 16


@dataclass
class PrintResult:
    success: bool
    error: str | None = None
    bytes_sent: int = 0


# ── Packbits compression ────────────────────────────────────────────

def packbits_encode(data: bytes) -> bytes:
    """TIFF-style packbits encoder matching what the Brother software
    sends. Format per byte:
      0x00..0x7F  →  copy next N+1 bytes literally
      0x81..0xFF  →  copy next byte (2's-complement signed) - n + 1 times
      0x80        →  no-op (we never emit this)
    """
    if not data:
        return b""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        # Look ahead for a run of identical bytes (min length 3 to encode)
        run = 1
        while i + run < n and data[i + run] == data[i] and run < 128:
            run += 1
        if run >= 3:
            out.append((257 - run) & 0xFF)
            out.append(data[i])
            i += run
            continue
        # Otherwise capture a literal run until we hit a 3+ repeat or EOL
        lit_start = i
        while i < n:
            r = 1
            while i + r < n and data[i + r] == data[i] and r < 3:
                r += 1
            if r >= 3:
                break
            i += 1
            if i - lit_start >= 128:
                break
        length = i - lit_start
        out.append(length - 1)
        out.extend(data[lit_start:i])
    return bytes(out)


# ── Raster line builder ─────────────────────────────────────────────

def _image_to_raster_lines(
    img: Image.Image, tape_width: str,
) -> tuple[list[bytes], int]:
    """Convert a PIL image to a list of 16-byte raster lines, one per
    column of the rendered tape. Returns (lines, label_length_dots).

    The image is assumed to be rendered landscape with height == tape
    printable dots and width == label length in dots at 180 DPI. We:
      1. resize/pad image height to the tape's printable dot count,
      2. embed it left-padded into a 128-bit raster column,
      3. emit one byte-string per column.
    """
    if tape_width not in TAPE_DOT_TABLE:
        raise ValueError(f"unsupported tape_width {tape_width!r}; "
                         f"known: {list(TAPE_DOT_TABLE)}")
    printable_dots, offset_dots = TAPE_DOT_TABLE[tape_width]

    # Ensure 1-bit black/white. PIL's "1" mode: 0 = black, 255 = white.
    bw = img.convert("1") if img.mode != "1" else img

    # Two valid input shapes:
    #  • height == printable_dots  → content fills the printable band;
    #    we left-pad to the head-centering offset.
    #  • height == HEAD_BYTES * 8 (128) → caller already positioned the
    #    content inside the full 128-dot head frame (labels.py does this
    #    so the same render works for the CUPS backend). We use offset 0
    #    and treat each column as the full 128-dot raster line.
    if bw.height == HEAD_BYTES * 8:
        printable_dots = HEAD_BYTES * 8
        offset_dots = 0
    elif bw.height != printable_dots:
        ratio = printable_dots / bw.height
        new_w = max(1, round(bw.width * ratio))
        bw = bw.resize((new_w, printable_dots), Image.LANCZOS).convert("1")

    width = bw.width
    px = bw.load()

    # Build one raster line per column (printed left-to-right = front-to-back of tape).
    lines: list[bytes] = []
    for col in range(width):
        bits = bytearray(HEAD_BYTES)
        for row in range(printable_dots):
            if px[col, row] == 0:  # black pixel
                # Insert at dot index (offset_dots + row); bit 7 is dot 0
                # of the first byte of the head.
                dot_idx = offset_dots + row
                byte_idx = dot_idx // 8
                bit_idx = 7 - (dot_idx % 8)
                if byte_idx < HEAD_BYTES:
                    bits[byte_idx] |= 1 << bit_idx
        lines.append(bytes(bits))
    return lines, width


# ── P-touch raster command stream ───────────────────────────────────

def build_data_file(
    images: list[Image.Image],
    tape_width: str,
    *,
    cut_at_end: bool = True,
    half_cut: bool = True,
    auto_cut: bool = False,
    margin_dots: int = 14,
    compression: str = "packbits",
) -> bytes:
    """Build the P-touch raster command stream (the LPR 'data file').

    Byte layout — verified against capture of Brother's official software:

      [0..99]   100 × 0x00            — invalidate / buffer flush
      [100]     1B 40                 — ESC @ initialize
      [102]     1B 69 61 01           — ESC i a 01 (raster mode)
      [106]     1B 69 55 4A + 14 b    — ESC i U J + job header (Brother
                                        extension; bytes replayed verbatim
                                        from capture; 0x3A in slot 11 looks
                                        like chain-max, rest is opaque)
      per page:
        ESC i z (10b)                 — print info: tape kind/width/length,
                                        raster-line count, page index
        ESC i M (1b)                  — auto-cut, mirror
        ESC i K (1b)                  — cut-at-end, half-cut
        ESC i d (2b)                  — leading margin in dots
        4D <comp>                     — compression mode (0=none, 2=tiff)
        47 <len_le> <packbits>×N  OR  — compressed raster lines
        47 <len_le> <16 raw>×N        — uncompressed raster lines
        1A (last page) or 0C          — print & eject / print & chain
    """
    if compression not in ("packbits", "none"):
        raise ValueError(f"compression must be 'packbits' or 'none'")

    buf = bytearray()

    # 1. Invalidate buffer
    buf += b"\x00" * 100

    # 2. Initialize + raster mode
    buf += b"\x1B\x40"
    buf += b"\x1B\x69\x61\x01"

    # 3. Brother extension (job header). Bytes replayed from capture;
    # these are required for the PT-E550W firmware to accept the job.
    # The 0x3A in position 11 (decimal 58) is plausibly the chain-max
    # but we haven't reverse-engineered the full meaning. Don't touch.
    buf += b"\x1B\x69\x55\x4A"
    buf += b"\x00\x0C\xFC\x34\x97\x10\x2B\xC8\x00\x00\x3A\x00\x00\x00"

    for page_idx, img in enumerate(images):
        is_last = (page_idx == len(images) - 1)
        lines, label_dots = _image_to_raster_lines(img, tape_width)

        # 4. Print info (ESC i z) — 10 parameter bytes
        # flags = 0x84 (bit2 = length valid in n3+n4, bit7 = quality)
        # kind  = 0x00 (laminated)
        # width = tape width in mm
        # length = 0 (auto; printer uses raster_lines count for length)
        # n3..n6 = raster_lines count (LE32)
        # n7 = starting page (0 = first)
        # n8 = fixed 0
        tape_mm = int(float(tape_width)) if tape_width != "3.5" else 4
        # Note: 3.5mm tapes report as 4mm in the print-info byte per
        # Brother docs (the firmware does its own narrow-tape mapping).
        buf += b"\x1B\x69\x7A"
        buf += bytes([
            0x84,                                   # flags
            0x00,                                   # media kind (laminated)
            tape_mm & 0xFF,                         # tape width mm
            0x00,                                   # tape length mm (auto)
        ])
        buf += struct.pack("<I", label_dots)        # raster line count
        buf += bytes([0x00 if page_idx == 0 else 0x01, 0x00])  # page index, fixed

        # 5. Mode bits
        m_bits = 0
        if auto_cut: m_bits |= 0x40
        # bit 0x80 is mirror; leave off
        buf += b"\x1B\x69\x4D" + bytes([m_bits])

        # 6. Extended mode bits
        k_bits = 0
        if half_cut:   k_bits |= 0x04
        if cut_at_end: k_bits |= 0x08
        buf += b"\x1B\x69\x4B" + bytes([k_bits])

        # 7. Leading margin (LE16, dots @ 180 DPI)
        buf += b"\x1B\x69\x64" + struct.pack("<H", margin_dots)

        # 8. Compression mode
        if compression == "packbits":
            buf += b"\x4D\x02"
            for line in lines:
                comp = packbits_encode(line)
                buf += b"\x47" + struct.pack("<H", len(comp)) + comp
        else:
            buf += b"\x4D\x00"
            for line in lines:
                buf += b"\x47" + struct.pack("<H", HEAD_BYTES) + line

        # 9. Page terminator
        buf += b"\x1A" if is_last else b"\x0C"

    return bytes(buf)


# ── LPD/LPR wrapper ─────────────────────────────────────────────────

def _build_control_file(job_id: int) -> bytes:
    """LPD control file (RFC 1179 §7). Tells the printer the job's
    metadata (host, user, filename) and references the data file."""
    jname = f"dfA{job_id:03d}{PT_HOST_NAME.decode()}".encode()
    lines = (
        b"H" + PT_HOST_NAME + b"\n"
        b"P" + PT_USER_NAME + b"\n"
        b"l" + jname + b"\n"   # 'l' = print as binary
        b"U" + jname + b"\n"   # 'U' = unlink (delete after print)
        b"N" + b"nexus.lbx\n"  # 'N' = job name (shown in printer queue)
    )
    return lines


def _lpd_send_sync(
    host: str,
    control_file: bytes,
    data_file: bytes,
    timeout: int = 30,
) -> int:
    """Blocking LPD send. Returns total bytes written. Raises on any
    protocol error or non-zero printer ack.

    LPD/LPR (RFC 1179) sub-protocol used by Brother's BINARY_P1 queue:
      → \x02 BINARY_P1 \n            (select queue)
      ← \x00                         (ack)
      → \x02 <ctl_size> cfA<id><host> \n
      ← \x00
      → <control file bytes> \x00
      ← \x00
      → \x03 <data_size> dfA<id><host> \n
      ← \x00
      → <data file bytes> \x00
      ← \x00
    """
    job_id = 58  # matches capture; any value 0-999 works
    cf_name = f"cfA{job_id:03d}{PT_HOST_NAME.decode()}".encode()
    df_name = f"dfA{job_id:03d}{PT_HOST_NAME.decode()}".encode()
    total = 0
    with socket.create_connection((host, LPD_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        # 1. select queue
        s.sendall(b"\x02" + LPD_QUEUE + b"\n")
        total += len(LPD_QUEUE) + 2
        ack = s.recv(1)
        if ack != b"\x00":
            raise RuntimeError(f"queue select failed; ack={ack!r}")
        # 2. control file header
        header = b"\x02" + str(len(control_file)).encode() + b" " + cf_name + b"\n"
        s.sendall(header)
        total += len(header)
        ack = s.recv(1)
        if ack != b"\x00":
            raise RuntimeError(f"control header reject; ack={ack!r}")
        # 3. control file body + null terminator
        s.sendall(control_file + b"\x00")
        total += len(control_file) + 1
        ack = s.recv(1)
        if ack != b"\x00":
            raise RuntimeError(f"control body reject; ack={ack!r}")
        # 4. data file header
        header = b"\x03" + str(len(data_file)).encode() + b" " + df_name + b"\n"
        s.sendall(header)
        total += len(header)
        ack = s.recv(1)
        if ack != b"\x00":
            raise RuntimeError(f"data header reject; ack={ack!r}")
        # 5. data file body + null terminator
        s.sendall(data_file + b"\x00")
        total += len(data_file) + 1
        ack = s.recv(1)
        if ack != b"\x00":
            raise RuntimeError(f"data body reject; ack={ack!r}")
    return total


# ── Public API ──────────────────────────────────────────────────────

async def print_labels(
    images: Iterable[Image.Image],
    host: str,
    tape_width: str = "12",
    *,
    cut_at_end: bool = True,
    half_cut: bool = True,
    auto_cut: bool = False,
    margin_dots: int = 14,
    compression: str = "packbits",
    **_kwargs,
) -> PrintResult:
    """Print one or more PIL Images to a PT-E550W via the raw LPD/P-touch
    protocol on ``host:515``. Multi-image jobs are sent as a single
    chained print (one cut at the end) by default.
    """
    image_list = [img for img in images if img is not None]
    target = f"{host}:{LPD_PORT}"
    if not image_list:
        log_api_command(
            system="brother_pt_raw", action="print", target=target,
            payload=f"tape={tape_width}mm",
            response_status="error", response_summary="no images to print",
        )
        return PrintResult(success=False, error="No images to print")

    try:
        data_file = build_data_file(
            image_list, tape_width,
            cut_at_end=cut_at_end, half_cut=half_cut, auto_cut=auto_cut,
            margin_dots=margin_dots, compression=compression,
        )
        control_file = _build_control_file(job_id=58)
        total = await asyncio.to_thread(
            _lpd_send_sync, host, control_file, data_file,
        )
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        logger.warning("PT-E550W raw LPD print failed: %s", err)
        log_api_command(
            system="brother_pt_raw", action="print", target=target,
            payload=f"tape={tape_width}mm images={len(image_list)}",
            response_status="error", response_summary=err,
        )
        return PrintResult(success=False, error=err)

    log_api_command(
        system="brother_pt_raw", action="print", target=target,
        payload=f"tape={tape_width}mm images={len(image_list)} bytes={total}",
        response_status="ok",
        response_summary=f"printed {len(image_list)} label(s)",
    )
    return PrintResult(success=True, bytes_sent=total)
