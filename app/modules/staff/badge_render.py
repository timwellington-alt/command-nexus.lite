"""
Temporary badge + credentials label renderer for the Brother QL-820NWB.

Produces two portrait CR80-style labels on 62mm continuous tape (DK-2205):

    1. Temp badge label  — photo, name, building, role
    2. Credentials label — name, email, temp password

Both labels are sized to match the proportions of a real ID card
(54mm × 85.6mm portrait) and rendered at 300 DPI. The photo is
pre-dithered with Floyd-Steinberg so it actually looks like a face
on a 1-bit thermal printer — naive thresholding produces blobs.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

logger = logging.getLogger(__name__)

# 62mm tape — brother_ql clamps the image width to 696 px at 300 dpi.
# We render the active content centered in 638 px (the CR80 portrait
# width, 54mm) and pad to 696. Label length along the tape is 85.6mm
# = 1012 px, matching CR80 portrait.
TAPE_WIDTH_PX = 696
CONTENT_WIDTH_PX = 638          # 54mm @ 300dpi
BADGE_HEIGHT_PX = 1012          # 85.6mm @ 300dpi
CREDENTIAL_HEIGHT_PX = 1012     # Same — matching ID card shape

_H_PAD = (TAPE_WIDTH_PX - CONTENT_WIDTH_PX) // 2  # 29 px left/right margin

# Fonts are resolved from the Pillow default if none of these exist.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_MONO_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]


def _load_font(size: int, mono: bool = False):
    from PIL import ImageFont

    candidates = _MONO_CANDIDATES if mono else _FONT_CANDIDATES
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _truncate_to_width(draw, text: str, font, max_width: int) -> str:
    """Trim text with an ellipsis so it fits in max_width pixels."""
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "\u2026"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis


def _fit_font_size(draw, text: str, base_size: int, max_width: int,
                   min_size: int = 40, mono: bool = False):
    """
    Find the largest font <= base_size whose text fits in max_width.
    Never returns a font smaller than min_size (text will get truncated
    with an ellipsis instead).
    """
    size = base_size
    while size > min_size:
        font = _load_font(size, mono=mono)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return _load_font(min_size, mono=mono)


def _prepare_photo_for_thermal(photo_path: str, target_w: int, target_h: int):
    """
    Load a color photo and optimize it for 1-bit thermal printing.

    Pipeline:
      1. Convert to grayscale.
      2. Auto-contrast (stretch histogram, clipping top/bottom 1%).
      3. Contrast boost so midtones don't collapse.
      4. Cover-fit resize + center crop to target size.
      5. Floyd-Steinberg dither to 1-bit.
      6. Re-expand to RGB so it pastes cleanly into the label canvas.

    The result is pure black/white with a dither pattern that simulates
    grayscale — much more legible on a thermal printer than naive
    thresholding, which turns faces into blotchy blobs.
    """
    from PIL import Image, ImageOps, ImageEnhance

    with Image.open(photo_path) as pimg:
        pimg = pimg.convert("L")
        pimg = ImageOps.autocontrast(pimg, cutoff=1)
        pimg = ImageEnhance.Contrast(pimg).enhance(1.35)
        pimg = ImageEnhance.Brightness(pimg).enhance(1.05)

        # Cover-fit
        ow, oh = pimg.size
        scale = max(target_w / ow, target_h / oh)
        nw = int(ow * scale)
        nh = int(oh * scale)
        pimg = pimg.resize((nw, nh), Image.LANCZOS)
        crop_x = (nw - target_w) // 2
        crop_y = (nh - target_h) // 2
        pimg = pimg.crop((crop_x, crop_y, crop_x + target_w, crop_y + target_h))

        # Floyd-Steinberg dither → 1-bit → back to RGB for pasting
        pimg = pimg.convert("1", dither=Image.FLOYDSTEINBERG)
        return pimg.convert("RGB")


def render_badge_label(
    first_name: str,
    last_name: str,
    preferred_name: str | None,
    building_name: str,
    title: str,
    photo_path: str | None,
    district_name: str,
) -> "Image.Image":
    """
    Temporary badge in CR80 portrait proportions (54 x 85.6 mm) on
    62mm tape. Layout top-to-bottom: banner, photo, name, building,
    title, footer.
    """
    from PIL import Image, ImageDraw

    W, H = TAPE_WIDTH_PX, BADGE_HEIGHT_PX
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    content_left = _H_PAD
    content_right = W - _H_PAD
    content_w = content_right - content_left

    # Thin border around the whole card so it looks like an actual badge
    draw.rectangle([(content_left - 4, 4), (content_right + 4, H - 4)],
                   outline="black", width=3)

    # ── Header banner ──
    banner_h = 90
    draw.rectangle([(content_left, 10), (content_right, 10 + banner_h)],
                   fill="black")
    banner_text = (district_name or "TEMPORARY BADGE").upper()
    banner_font = _fit_font_size(draw, banner_text, base_size=56,
                                 max_width=content_w - 24, min_size=32)
    bw = draw.textlength(banner_text, font=banner_font)
    # Vertically center the banner text
    bbox = banner_font.getbbox(banner_text)
    bh = bbox[3] - bbox[1]
    draw.text(
        (content_left + (content_w - bw) / 2, 10 + (banner_h - bh) / 2 - bbox[1]),
        banner_text,
        font=banner_font,
        fill="white",
    )

    # ── Photo ──
    photo_top = 10 + banner_h + 24
    photo_w = 460
    photo_h = 500
    photo_x = content_left + (content_w - photo_w) // 2
    photo_box = (photo_x, photo_top, photo_x + photo_w, photo_top + photo_h)
    draw.rectangle(photo_box, outline="black", width=4)

    if photo_path and os.path.exists(photo_path):
        try:
            thermal_photo = _prepare_photo_for_thermal(
                photo_path, photo_w - 8, photo_h - 8,
            )
            img.paste(thermal_photo, (photo_x + 4, photo_top + 4))
        except Exception as e:
            logger.warning(f"Failed to prepare badge photo {photo_path}: {e}")
            _draw_photo_placeholder(draw, photo_box)
    else:
        _draw_photo_placeholder(draw, photo_box)

    # ── Name ──
    display_first = (preferred_name or first_name or "").strip()
    display_name = f"{display_first} {last_name or ''}".strip()

    name_y = photo_top + photo_h + 30
    name_font = _fit_font_size(draw, display_name, base_size=96,
                               max_width=content_w - 20, min_size=56)
    nw = draw.textlength(display_name, font=name_font)
    draw.text(
        (content_left + (content_w - nw) / 2, name_y),
        display_name,
        font=name_font,
        fill="black",
    )

    # ── Building ──
    bldg_y = name_y + 110
    bldg_text = (building_name or "").strip().upper()
    bldg_font = _fit_font_size(draw, bldg_text, base_size=52,
                               max_width=content_w - 20, min_size=32)
    bldgw = draw.textlength(bldg_text, font=bldg_font)
    draw.text(
        (content_left + (content_w - bldgw) / 2, bldg_y),
        bldg_text,
        font=bldg_font,
        fill="black",
    )

    # ── Title ──
    title_y = bldg_y + 62
    title_text = (title or "").strip()
    if title_text:
        title_font = _fit_font_size(draw, title_text, base_size=42,
                                    max_width=content_w - 20, min_size=28)
        tw = draw.textlength(title_text, font=title_font)
        draw.text(
            (content_left + (content_w - tw) / 2, title_y),
            title_text,
            font=title_font,
            fill="black",
        )

    # ── Footer ──
    footer_h = 58
    draw.rectangle(
        [(content_left, H - footer_h - 10), (content_right, H - 10)],
        fill="black",
    )
    footer_text = "TEMPORARY — REPLACE WITH PERMANENT ID"
    footer_font = _fit_font_size(draw, footer_text, base_size=32,
                                 max_width=content_w - 24, min_size=22)
    fw = draw.textlength(footer_text, font=footer_font)
    bbox = footer_font.getbbox(footer_text)
    fh = bbox[3] - bbox[1]
    draw.text(
        (content_left + (content_w - fw) / 2,
         H - footer_h - 10 + (footer_h - fh) / 2 - bbox[1]),
        footer_text,
        font=footer_font,
        fill="white",
    )

    return img


def _draw_photo_placeholder(draw, box):
    x1, y1, x2, y2 = box
    draw.line([(x1, y1), (x2, y2)], fill="#999999", width=3)
    draw.line([(x2, y1), (x1, y2)], fill="#999999", width=3)
    font = _load_font(36)
    label = "NO PHOTO"
    tw = draw.textlength(label, font=font)
    bbox = font.getbbox(label)
    fh = bbox[3] - bbox[1]
    draw.text(
        ((x1 + x2) / 2 - tw / 2, (y1 + y2) / 2 - fh / 2 - bbox[1]),
        label,
        font=font,
        fill="#666666",
    )


def render_credentials_label(
    first_name: str,
    last_name: str,
    email: str,
    temp_password: str,
    district_name: str,
) -> "Image.Image":
    """
    Credentials label — same CR80 portrait footprint as the badge.
    Big monospace password so every character is unambiguous.
    """
    from PIL import Image, ImageDraw

    W, H = TAPE_WIDTH_PX, CREDENTIAL_HEIGHT_PX
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    content_left = _H_PAD
    content_right = W - _H_PAD
    content_w = content_right - content_left

    # Outer border
    draw.rectangle([(content_left - 4, 4), (content_right + 4, H - 4)],
                   outline="black", width=3)

    # ── Header banner ──
    banner_h = 90
    draw.rectangle([(content_left, 10), (content_right, 10 + banner_h)],
                   fill="black")
    banner_text = "ACCOUNT CREDENTIALS"
    banner_font = _fit_font_size(draw, banner_text, base_size=50,
                                 max_width=content_w - 24, min_size=32)
    bw = draw.textlength(banner_text, font=banner_font)
    bbox = banner_font.getbbox(banner_text)
    bh = bbox[3] - bbox[1]
    draw.text(
        (content_left + (content_w - bw) / 2, 10 + (banner_h - bh) / 2 - bbox[1]),
        banner_text,
        font=banner_font,
        fill="white",
    )

    # ── Name ──
    y = 10 + banner_h + 36
    display_name = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    name_font = _fit_font_size(draw, display_name, base_size=80,
                               max_width=content_w - 20, min_size=48)
    nw = draw.textlength(display_name, font=name_font)
    draw.text(
        (content_left + (content_w - nw) / 2, y),
        display_name,
        font=name_font,
        fill="black",
    )
    y += 110

    # ── Email ──
    label_font = _load_font(34)
    draw.text((content_left, y), "EMAIL", font=label_font, fill="#555555")
    y += 42
    email_text = email or ""
    email_font = _fit_font_size(draw, email_text, base_size=40,
                                max_width=content_w, min_size=24, mono=True)
    draw.text((content_left, y), email_text, font=email_font, fill="black")
    y += 90

    # ── Password box ──
    draw.text((content_left, y), "TEMP PASSWORD", font=label_font, fill="#555555")
    y += 42
    pw_text = temp_password or "(not generated)"
    pw_font = _fit_font_size(draw, pw_text, base_size=68,
                             max_width=content_w - 40, min_size=40, mono=True)
    pw_w = draw.textlength(pw_text, font=pw_font)
    pw_bbox = pw_font.getbbox(pw_text)
    pw_h = pw_bbox[3] - pw_bbox[1]
    # Draw a framed box around the password for emphasis
    box_pad_x = 20
    box_pad_y = 16
    box = (
        content_left,
        y,
        content_right,
        y + pw_h + 2 * box_pad_y + 8,
    )
    draw.rectangle(box, outline="black", width=4)
    draw.text(
        (content_left + (content_w - pw_w) / 2,
         y + box_pad_y - pw_bbox[1]),
        pw_text,
        font=pw_font,
        fill="black",
    )
    y = box[3] + 30

    # ── Footer reminder ──
    footer_font = _load_font(26)
    lines = [
        "Change password on first sign-in.",
        "Enable 2-step verification.",
    ]
    line_gap = 8
    line_heights = []
    for line in lines:
        bb = footer_font.getbbox(line)
        line_heights.append(bb[3] - bb[1])
    total_footer_h = sum(line_heights) + line_gap * (len(lines) - 1)
    fy = H - 30 - total_footer_h
    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=footer_font)
        draw.text(
            (content_left + (content_w - lw) / 2, fy),
            line,
            font=footer_font,
            fill="#333333",
        )
        fy += line_heights[i] + line_gap

    return img


def render_combined_job(
    first_name: str,
    last_name: str,
    preferred_name: str | None,
    email: str,
    temp_password: str,
    building_name: str,
    title: str,
    photo_path: str | None,
    district_name: str,
) -> list:
    """
    Build the full two-label print job — badge first, credentials
    second. brother_ql auto-cut produces two physically separate
    labels from a single send.
    """
    badge = render_badge_label(
        first_name=first_name,
        last_name=last_name,
        preferred_name=preferred_name,
        building_name=building_name,
        title=title,
        photo_path=photo_path,
        district_name=district_name,
    )
    creds = render_credentials_label(
        first_name=first_name,
        last_name=last_name,
        email=email,
        temp_password=temp_password,
        district_name=district_name,
    )
    return [badge, creds]
