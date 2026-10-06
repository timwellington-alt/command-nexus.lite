"""Avery label sheet PDF generators.

Three supported templates (all portrait, 8.5" x 11" US-letter):

  - avery_5160 — 30 labels, 3 across × 10 down, 1" × 2-5/8"
  - avery_5161 — 20 labels, 2 across × 10 down, 1" × 4"
  - avery_8160 — same layout as 5160, white shipping stock

Each label renders:
  - Human-readable heading (asset_tag or serial)
  - Secondary line (mfr + model, truncated)
  - Code128 barcode of the primary ID
  - Optional "property of <district>" footer

No QR codes — we're using linear barcodes to match the scanner's
default format list and the hardware USB wedge scanners we test with.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterable

from reportlab.graphics.barcode import code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


PAGE_W, PAGE_H = LETTER  # 8.5 * 11 inch at 72 dpi


@dataclass(frozen=True)
class Template:
    name: str
    cols: int
    rows: int
    label_w: float  # inches
    label_h: float
    margin_l: float
    margin_t: float
    col_gap: float
    row_gap: float


TEMPLATES: dict[str, Template] = {
    "avery_5160": Template(
        name="Avery 5160", cols=3, rows=10,
        label_w=2.625, label_h=1.0,
        margin_l=0.1875, margin_t=0.5,
        col_gap=0.125, row_gap=0.0,
    ),
    "avery_5161": Template(
        name="Avery 5161", cols=2, rows=10,
        label_w=4.0, label_h=1.0,
        margin_l=0.15625, margin_t=0.5,
        col_gap=0.1875, row_gap=0.0,
    ),
    "avery_8160": Template(
        name="Avery 8160", cols=3, rows=10,
        label_w=2.625, label_h=1.0,
        margin_l=0.1875, margin_t=0.5,
        col_gap=0.125, row_gap=0.0,
    ),
    # Custom layout for QR-code shelf/container labels. Not a commercial
    # Avery sheet — user prints on plain paper and cuts/tapes onto shelves.
    # 2.5" × 2.5" each, 3 cols × 4 rows = 12 per letter sheet, with enough
    # margin to trim by hand.
    "shelf_qr": Template(
        name="Shelf QR (3×4, cut to size)", cols=3, rows=4,
        label_w=2.5, label_h=2.5,
        margin_l=0.5, margin_t=0.5,
        col_gap=0.25, row_gap=0.25,
    ),
}


@dataclass
class LabelPayload:
    primary: str                      # what's on the barcode (asset_tag or serial)
    heading: str                      # larger human line (same as primary, usually)
    subheading: str | None = None     # mfr + model or description
    footer: str | None = None         # e.g. "Property of <District>"


def _label_origin(tpl: Template, idx: int) -> tuple[float, float]:
    col = idx % tpl.cols
    row = idx // tpl.cols
    x = (tpl.margin_l + col * (tpl.label_w + tpl.col_gap)) * inch
    # PDFs use bottom-left origin; reportlab canvas respects that.
    y_from_top = (tpl.margin_t + row * (tpl.label_h + tpl.row_gap)) * inch
    y = PAGE_H - y_from_top - tpl.label_h * inch
    return x, y


def _draw_label(c: canvas.Canvas, tpl: Template, x: float, y: float, payload: LabelPayload) -> None:
    w = tpl.label_w * inch
    h = tpl.label_h * inch

    # Heading — sized to fit. Bold, truncated if too long.
    c.setFont("Helvetica-Bold", 10)
    heading = payload.heading or ""
    if c.stringWidth(heading, "Helvetica-Bold", 10) > w - 8:
        # Shrink incrementally
        for sz in (9, 8, 7):
            if c.stringWidth(heading, "Helvetica-Bold", sz) <= w - 8:
                c.setFont("Helvetica-Bold", sz)
                break
        else:
            # Truncate with ellipsis
            while heading and c.stringWidth(heading + "…", "Helvetica-Bold", 7) > w - 8:
                heading = heading[:-1]
            heading = heading + "…"
    c.drawString(x + 4, y + h - 13, heading)

    # Subheading
    if payload.subheading:
        c.setFont("Helvetica", 7)
        sub = payload.subheading
        while sub and c.stringWidth(sub, "Helvetica", 7) > w - 8:
            sub = sub[:-1]
        if sub != payload.subheading:
            sub = sub[:-1] + "…"
        c.drawString(x + 4, y + h - 22, sub)

    # Barcode — centered, fixed height
    primary = (payload.primary or "").strip() or " "
    # Pick a barWidth small enough that the full code fits within the label width
    avail = w - 12
    for bw in (1.3, 1.1, 0.95, 0.8, 0.68):
        bc = code128.Code128(primary, barWidth=bw, barHeight=0.32 * inch, humanReadable=False)
        if bc.width <= avail:
            break
    bc_x = x + (w - bc.width) / 2
    bc_y = y + 8
    bc.drawOn(c, bc_x, bc_y)

    # Footer
    if payload.footer:
        c.setFont("Helvetica", 5.5)
        foot = payload.footer
        while foot and c.stringWidth(foot, "Helvetica", 5.5) > w - 8:
            foot = foot[:-1]
        c.drawString(x + 4, y + 2, foot)


def render_sheet(template: str, labels: Iterable[LabelPayload]) -> bytes:
    """Produce a PDF sheet. Multiple pages are emitted if labels exceed
    cols*rows per sheet."""
    tpl = TEMPLATES.get(template)
    if not tpl:
        raise ValueError(f"Unknown template: {template}")
    per_sheet = tpl.cols * tpl.rows
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    label_list = list(labels)
    for i, payload in enumerate(label_list):
        slot = i % per_sheet
        if i > 0 and slot == 0:
            c.showPage()
        x, y = _label_origin(tpl, slot)
        _draw_label(c, tpl, x, y, payload)
    c.showPage()
    c.save()
    return buf.getvalue()


# ── QR label sheets (shelves + containers) ───────────────────────────────

@dataclass
class QrLabelPayload:
    """One QR label on a sheet. qr_value is what gets encoded (e.g.
    'SHELF:A-3' or 'BOX:Cables-17'); heading/subheading are the
    human-readable lines rendered below the QR."""
    qr_value: str
    heading: str
    subheading: str | None = None


def _place_qr(c: canvas.Canvas, value: str, x: float, y: float, side: float) -> None:
    """Scale a QR widget to a square of `side` pts and draw at (x, y)."""
    qr = QrCodeWidget(value, barLevel="M")
    bounds = qr.getBounds()
    bw = bounds[2] - bounds[0]
    bh = bounds[3] - bounds[1]
    scale = side / max(bw, bh)
    d = Drawing(side, side, transform=[scale, 0, 0, scale, -bounds[0] * scale, -bounds[1] * scale])
    d.add(qr)
    d.drawOn(c, x, y)


def _draw_qr_label(c: canvas.Canvas, tpl: Template, x: float, y: float, p: QrLabelPayload) -> None:
    # Blank payloads are used to pad the sheet for start_offset — draw nothing.
    if not p.qr_value:
        return
    w = tpl.label_w * inch
    h = tpl.label_h * inch

    # Only draw cut guides on the custom "shelf_qr" template — Avery sheets
    # already have pre-cut labels, so borders would waste toner.
    if tpl.name.startswith("Shelf QR"):
        c.setStrokeColorRGB(0.75, 0.75, 0.75)
        c.setLineWidth(0.4)
        c.rect(x, y, w, h, stroke=1, fill=0)
        c.setStrokeColorRGB(0, 0, 0)

    landscape = w > h * 1.3  # narrow-tall labels get side-by-side layout

    if landscape:
        # Side-by-side: QR left, text right. Fits Avery 5160 (1"×2⅝") etc.
        pad = 4
        qr_side = h - pad * 2
        qr_x = x + pad
        qr_y = y + pad
        _place_qr(c, p.qr_value, qr_x, qr_y, qr_side)

        text_left = qr_x + qr_side + 6
        text_w = w - (text_left - x) - pad

        heading = (p.heading or "").strip()
        c.setFont("Helvetica-Bold", 11)
        for sz in (11, 10, 9, 8):
            if c.stringWidth(heading, "Helvetica-Bold", sz) <= text_w:
                c.setFont("Helvetica-Bold", sz)
                break
        else:
            while heading and c.stringWidth(heading + "…", "Helvetica-Bold", 8) > text_w:
                heading = heading[:-1]
            heading += "…"
            c.setFont("Helvetica-Bold", 8)
        c.drawString(text_left, y + h - 14, heading)

        if p.subheading:
            sub = p.subheading.strip()
            c.setFont("Helvetica", 7)
            while sub and c.stringWidth(sub, "Helvetica", 7) > text_w:
                sub = sub[:-1]
            c.setFillColorRGB(0.4, 0.4, 0.4)
            c.drawString(text_left, y + h - 25, sub)
            c.setFillColorRGB(0, 0, 0)
        return

    # Stacked: QR on top, text below. For square-ish custom labels.
    qr_side = min(w - 10, h * 0.70)
    qr_x = x + (w - qr_side) / 2
    qr_y = y + h - qr_side - 6
    _place_qr(c, p.qr_value, qr_x, qr_y, qr_side)

    heading = (p.heading or "").strip()
    c.setFont("Helvetica-Bold", 12)
    if c.stringWidth(heading, "Helvetica-Bold", 12) > w - 6:
        for sz in (11, 10, 9):
            if c.stringWidth(heading, "Helvetica-Bold", sz) <= w - 6:
                c.setFont("Helvetica-Bold", sz)
                break
    tx = x + w / 2
    c.drawCentredString(tx, y + h - qr_side - 22, heading)

    if p.subheading:
        sub = p.subheading.strip()
        c.setFont("Helvetica", 8)
        while sub and c.stringWidth(sub, "Helvetica", 8) > w - 6:
            sub = sub[:-1]
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawCentredString(tx, y + h - qr_side - 34, sub)
        c.setFillColorRGB(0, 0, 0)


def render_qr_sheet(labels: Iterable[QrLabelPayload], template: str = "shelf_qr") -> bytes:
    """PDF sheet of QR labels (shelves, containers, etc.). Each label has
    a QR, a bold heading, and an optional subheading. Uses the shelf_qr
    template by default — 12 per letter sheet, cut by hand."""
    tpl = TEMPLATES.get(template)
    if not tpl:
        raise ValueError(f"Unknown template: {template}")
    per_sheet = tpl.cols * tpl.rows
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    label_list = list(labels)
    for i, p in enumerate(label_list):
        slot = i % per_sheet
        if i > 0 and slot == 0:
            c.showPage()
        x, y = _label_origin(tpl, slot)
        _draw_qr_label(c, tpl, x, y, p)
    c.showPage()
    c.save()
    return buf.getvalue()
