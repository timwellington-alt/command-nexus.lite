"""
Paxton Net2 CardDesigner XML → PNG/PDF renderer.

Parses the district's CardDesigner template (Project Specs/Paxton/the district.xml)
and produces a printable ID card image populated with live staff data.
Output is CR80 (85.6mm × 53.98mm) at 300 DPI = 1011 × 638 px.

Design notes:
- Paxton's REST API doesn't expose card printing, so this module ONLY
  renders. Operator downloads + prints via their local printer.
- Fonts: Arial + Verdana aren't in the container by default. We substitute
  DejaVu Sans, which is metric-close. If Tim wants exact Windows visuals,
  install ttf-mscorefonts-installer (EULA required).
- Colors in the XML are .NET ARGB ints as SIGNED 32-bit — see
  `_argb_from_int` for the decode.
- Only the fields the district template uses are wired here; adding
  new LiveFields is a one-line addition to `_resolve_live_field`.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# CR80 card at 300 DPI
DPI = 300
MM_TO_PX = DPI / 25.4  # 11.811
CARD_WIDTH_MM = 85.6
CARD_HEIGHT_MM = 53.98
CARD_WIDTH_PX = round(CARD_WIDTH_MM * MM_TO_PX)   # 1011
CARD_HEIGHT_PX = round(CARD_HEIGHT_MM * MM_TO_PX)  # 638

# Font substitution table. Windows fonts (Arial, Verdana) aren't in the
# API container; DejaVu Sans is the closest OFL font that ships with
# fontconfig on Debian.
_FONT_SUBSTITUTES = {
    "arial":   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "verdana": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "times":   "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "courier": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
}
_FONT_SUBSTITUTES_BOLD = {
    "arial":   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "verdana": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "times":   "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "courier": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
}
_DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DEFAULT_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _argb_from_int(v: int | str) -> tuple[int, int, int, int]:
    """Convert a .NET ARGB signed 32-bit int → (R, G, B, A).

    In Paxton's XML colors like `-1` (white) and `-13683047` (dark teal)
    are Color.ToArgb() output. The int is signed but the bit pattern is
    AARRGGBB. Convert by masking.
    """
    if isinstance(v, str):
        v = int(v)
    n = v & 0xFFFFFFFF  # unsigned view
    a = (n >> 24) & 0xFF
    r = (n >> 16) & 0xFF
    g = (n >> 8) & 0xFF
    b = n & 0xFF
    # Paxton often uses 0 to mean fully transparent in BackColour AND
    # sometimes to mean "no fill." Treat alpha=0 as fully transparent
    # (which Pillow will honor when composited over RGBA).
    return (r, g, b, a)


def _pt_to_px(pt: float) -> int:
    """Point → pixel at DPI. 1pt = 1/72 inch."""
    return round(pt * DPI / 72.0)


def _mm_to_px(mm: float) -> int:
    return round(mm * MM_TO_PX)


@lru_cache(maxsize=32)
def _load_font(name: str, size_pt: int, bold: bool) -> Any:
    """Pillow font loader with substitution + graceful fallback."""
    from PIL import ImageFont

    key = (name or "").lower()
    table = _FONT_SUBSTITUTES_BOLD if bold else _FONT_SUBSTITUTES
    path = table.get(key) or (_DEFAULT_FONT_BOLD if bold else _DEFAULT_FONT)
    try:
        return ImageFont.truetype(path, _pt_to_px(size_pt))
    except OSError:
        logger.warning("Font %s not loadable at %s, using Pillow default", name, path)
        return ImageFont.load_default()


@dataclass
class _Item:
    """Parsed DisplayItem in a friendlier shape."""
    kind: str
    props: dict[str, str]


@dataclass
class _Template:
    """Parsed template — items in z-order + image asset lookup."""
    items: list[_Item]
    images: dict[str, bytes]  # image_id → PNG bytes


@lru_cache(maxsize=1)
def _load_template(path: str) -> _Template:
    """Parse the CardDesigner XML once and cache."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # File declares utf-16 but is actually saved as utf-8 no-BOM. Strip
    # the misleading XML declaration so ET parses it as-is.
    content = re.sub(r"^<\?xml[^>]+\?>\s*", "", content)
    root = ET.fromstring(content)

    images: dict[str, bytes] = {}
    for img in root.findall("./Images/Image"):
        img_id = img.get("Id", "")
        b64 = (img.text or "").strip()
        try:
            images[img_id] = base64.b64decode(b64)
        except Exception as e:
            logger.warning("Bad image asset %s: %s", img_id, e)

    items: list[_Item] = []
    for el in root.findall("./DisplayItems/DisplayItem"):
        kind = (el.get("ItemType") or "").replace("Paxton.Net2.CardDesigner.", "")
        props: dict[str, str] = {}
        for prop in el.findall("./Property"):
            name = prop.get("name")
            value = prop.get("value", "")
            if name:
                props[name] = value
        items.append(_Item(kind=kind, props=props))

    return _Template(items=items, images=images)


def _resolve_live_field(field: str, ctx: dict) -> str:
    """Map a Paxton LiveField spec to a rendered string using `ctx`.

    Handles both plain LiveField names ("DepartmentName") and Paxton's
    compound "F, S, First, Middle, Surname" format (rendered as "First
    Last" — the LiveFormat 300012 code is Paxton's default name format).
    """
    if not field:
        return ""
    # Compound name field
    if "," in field:
        first = ctx.get("first_name") or ""
        last = ctx.get("last_name") or ""
        return f"{first} {last}".strip()
    key = field.strip().lower()
    if key == "departmentname":
        return ctx.get("department") or ctx.get("building_display") or ""
    if key == "firstname":
        return ctx.get("first_name") or ""
    if key == "surname":
        return ctx.get("last_name") or ""
    if key == "middlename":
        return ctx.get("middle_name") or ""
    if key.startswith("field") and "_" in key:
        # Paxton custom field spec like "Field10_50" — number after
        # "Field" is the id, number after "_" is the max length hint.
        try:
            fid = int(re.match(r"field(\d+)", key).group(1))
        except (AttributeError, ValueError):
            return ""
        cfs = ctx.get("custom_fields") or {}
        return str(cfs.get(fid) or "")
    logger.warning("Unmapped LiveField: %s", field)
    return ""


def _draw_content_image(img, item_props, ctx):
    """ContentImageItem: renders the cardholder photo (LiveField=ImageData)."""
    from PIL import Image
    x = _mm_to_px(float(item_props.get("LocationX", 0)))
    y = _mm_to_px(float(item_props.get("LocationY", 0)))
    w = _mm_to_px(float(item_props.get("Width", 0)))
    h = _mm_to_px(float(item_props.get("Height", 0)))
    fill_mode = item_props.get("ImageFill", "Fit")

    photo_path = ctx.get("photo_path")
    if not photo_path or not os.path.exists(photo_path):
        # Draw a placeholder rectangle so it's obvious a photo is missing
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.rectangle([x, y, x + w, y + h], outline=(120, 120, 120, 255), width=2)
        d.line([(x, y), (x + w, y + h)], fill=(160, 160, 160, 255), width=2)
        d.line([(x + w, y), (x, y + h)], fill=(160, 160, 160, 255), width=2)
        return

    try:
        photo = Image.open(photo_path).convert("RGBA")
    except Exception as e:
        logger.warning("Failed to load photo %s: %s", photo_path, e)
        return

    if fill_mode == "Stretch":
        photo = photo.resize((w, h), Image.LANCZOS)
    else:
        # Fit — preserve aspect, center inside box
        pw, ph = photo.size
        scale = min(w / pw, h / ph)
        nw, nh = int(pw * scale), int(ph * scale)
        photo = photo.resize((nw, nh), Image.LANCZOS)
        offset_x = x + (w - nw) // 2
        offset_y = y + (h - nh) // 2
        img.alpha_composite(photo, (offset_x, offset_y))
        return
    img.alpha_composite(photo, (x, y))


def _draw_static_image(img, item_props, template, ctx):
    """ImageItem: embedded PNG or a solid background rectangle when
    ImageId is blank (which the template uses for a card-wide fill)."""
    from PIL import Image, ImageDraw
    x = _mm_to_px(float(item_props.get("LocationX", 0)))
    y = _mm_to_px(float(item_props.get("LocationY", 0)))
    w = _mm_to_px(float(item_props.get("Width", 0)))
    h = _mm_to_px(float(item_props.get("Height", 0)))
    image_id = (item_props.get("ImageId") or "").strip()
    transparency = int(item_props.get("Transparency", 100))
    fill_mode = item_props.get("ImageFill", "Fit")

    if not image_id:
        # No image — treat as a color-filled background rectangle. The
        # template uses BackColour + GradientFill for this. We render
        # a simple gradient or solid fill.
        back = _argb_from_int(item_props.get("BackColour", "-1"))
        fore = _argb_from_int(item_props.get("ForeColour", "-1"))
        if back[3] == 0:
            return  # fully transparent
        # Simple horizontal gradient (Left_Right is the only mode in the
        # district template). Fallback to solid `back` when gradient is
        # disabled.
        if item_props.get("GradientFill") == "Left_Right" and fore[3] > 0:
            grad = Image.new("RGBA", (w, h))
            for gx in range(w):
                t = gx / max(w - 1, 1)
                r = int(back[0] * (1 - t) + fore[0] * t)
                g = int(back[1] * (1 - t) + fore[1] * t)
                b = int(back[2] * (1 - t) + fore[2] * t)
                a = int(back[3] * (1 - t) + fore[3] * t)
                for gy in range(h):
                    grad.putpixel((gx, gy), (r, g, b, a))
            img.alpha_composite(grad, (x, y))
        else:
            solid = Image.new("RGBA", (w, h), back)
            img.alpha_composite(solid, (x, y))
        return

    png = template.images.get(image_id)
    if not png:
        logger.warning("ImageId %s not in template", image_id)
        return
    logo = Image.open(io.BytesIO(png)).convert("RGBA")

    if fill_mode == "Stretch":
        logo = logo.resize((w, h), Image.LANCZOS)
    else:
        pw, ph = logo.size
        scale = min(w / pw, h / ph)
        nw, nh = int(pw * scale), int(ph * scale)
        logo = logo.resize((nw, nh), Image.LANCZOS)
        cx = x + (w - nw) // 2
        cy = y + (h - nh) // 2
        x, y = cx, cy

    # Apply transparency (0-100) as an alpha multiplier
    if transparency < 100:
        alpha = logo.split()[3]
        alpha = alpha.point(lambda p: int(p * transparency / 100))
        logo.putalpha(alpha)
    img.alpha_composite(logo, (x, y))


def _fit_text_font(draw, text, font_name, base_pt, bold, max_width, min_pt=8):
    """Return the largest font ≤ base_pt whose rendered width for `text`
    fits in max_width px. Steps down 1pt at a time; never below min_pt."""
    size = base_pt
    while size > min_pt:
        font = _load_font(font_name, size, bold)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 1
    return _load_font(font_name, min_pt, bold)


def _draw_text(img, item_props, ctx):
    """TextItem or ContentTextItem — static or LiveField-bound text.

    Auto-shrinks the font when the rendered string would exceed the
    item's Width, down to a floor of 8pt. Matches Paxton's Windows
    behavior for the same template.
    """
    from PIL import ImageDraw
    x = _mm_to_px(float(item_props.get("LocationX", 0)))
    y = _mm_to_px(float(item_props.get("LocationY", 0)))
    w = _mm_to_px(float(item_props.get("Width", 0)))
    h = _mm_to_px(float(item_props.get("Height", 0)))
    align = (item_props.get("TextAlignment") or "Left").lower()
    font_name = item_props.get("FontName", "Arial")
    base_size = int(item_props.get("FontSize", 12))
    bold = item_props.get("FontBold", "False").lower() == "true"
    fore = _argb_from_int(item_props.get("ForeColour", "-16777216"))

    live = (item_props.get("LiveField") or "").strip()
    text = _resolve_live_field(live, ctx) if live else item_props.get("Text", "")
    if not text:
        return

    d = ImageDraw.Draw(img)
    # A little horizontal breathing room so the text doesn't kiss the
    # item boundary; the template's boxes are tight against neighboring
    # elements and the PVC printer's stroke bleeds slightly.
    padding = _mm_to_px(1.0)
    font = _fit_text_font(d, text, font_name, base_size, bold,
                          max_width=max(w - padding, 1))

    tw = d.textlength(text, font=font)
    bbox = font.getbbox(text)
    th = bbox[3] - bbox[1]

    if align in ("centre", "center"):
        tx = x + (w - tw) // 2
    elif align == "right":
        tx = x + w - tw
    else:
        tx = x
    ty = y + (h - th) // 2 - bbox[1]

    d.text((tx, ty), text, font=font, fill=fore)


def render_card(ctx: dict, template_path: str | None = None):
    """Render a filled-in ID card for `ctx` and return a PIL Image (RGBA).

    ctx keys used:
        first_name, last_name, middle_name (str)
        department (str) — falls back to building_display
        building_display (str)
        photo_path (str) — filesystem path to jpg/png
        custom_fields (dict[int, str]) — Paxton custom field id → value
    """
    from PIL import Image

    if template_path is None:
        template_path = os.path.join(
            os.path.dirname(__file__), "paxton_templates", "the district.xml",
        )
    template = _load_template(os.path.abspath(template_path))

    # White base, matches a physical PVC card
    img = Image.new("RGBA", (CARD_WIDTH_PX, CARD_HEIGHT_PX), (255, 255, 255, 255))

    for it in template.items:
        try:
            if it.kind == "CardItem":
                # Card outline — no draw needed; canvas IS the card.
                continue
            elif it.kind == "ImageItem":
                _draw_static_image(img, it.props, template, ctx)
            elif it.kind == "ContentImageItem":
                _draw_content_image(img, it.props, ctx)
            elif it.kind in ("TextItem", "ContentTextItem"):
                _draw_text(img, it.props, ctx)
            else:
                logger.warning("Unhandled Paxton item kind: %s", it.kind)
        except Exception as e:
            logger.exception("Failed to render %s: %s", it.kind, e)

    return img


def render_card_pdf(ctx: dict, template_path: str | None = None) -> bytes:
    """Render and return a print-ready PDF (single page, exact card size)."""
    from PIL import Image
    img = render_card(ctx, template_path)
    # Flatten to RGB — PDF export loses alpha and CR80 has no printable
    # transparent regions on physical stock.
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    # `resolution` in Pillow's PDF writer sets the page size (page_size
    # = image_size / resolution × 72 pts). Using 300 DPI keeps the page
    # exactly CR80 at print time.
    flat.save(buf, format="PDF", resolution=float(DPI))
    return buf.getvalue()


def build_ctx_for_staff(staff_row: dict, paxton_row: dict | None = None) -> dict:
    """Assemble a render ctx from a staff_directory row + optional
    paxton_user_cache row + Paxton custom fields."""
    ctx = {
        "first_name": staff_row.get("first_name") or "",
        "last_name": staff_row.get("last_name") or "",
        "middle_name": staff_row.get("middle_name") or "",
        "department": staff_row.get("department") or "",
        "building_display": staff_row.get("building_display") or staff_row.get("building") or "",
        "photo_path": staff_row.get("photo_path"),
        "custom_fields": staff_row.get("custom_fields") or {},
    }
    if paxton_row:
        ctx["custom_fields"] = paxton_row.get("custom_fields") or ctx["custom_fields"]
        if not ctx["photo_path"] and paxton_row.get("paxton_id"):
            candidate = f"/app/data/photos/paxton_{paxton_row['paxton_id']}.jpg"
            if os.path.exists(candidate):
                ctx["photo_path"] = candidate
    return ctx
