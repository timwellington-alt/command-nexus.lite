"""
ID-card photo processor.

Prep pipeline for staff photos before they hit an ID card:
  1. EXIF-orient (fixes phone photos that come in sideways)
  2. Face detection via OpenCV Haar cascade (bundled with cv2)
  3. Crop centered on the detected face with headroom, target CR80
     badge photo aspect (~0.94:1); falls back to plain center-crop
     when no face is found or detection times out
  4. Resize to 600 × 640 px (roughly matches the CardDesigner
     ContentImage box at 300 DPI, so the ID card render has more
     than enough pixels)

Called from the photo-upload endpoints; returns a Pillow Image plus a
metadata dict so the caller can decide whether the photo is "ready"
enough to advance the staff_queue.id_card_status.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Aspect from the the district.xml ContentImage box (28.57mm × 30.43mm).
TARGET_ASPECT = 28.57 / 30.43   # ~0.939
TARGET_W = 600
TARGET_H = round(TARGET_W / TARGET_ASPECT)   # 639
# The loose auto-advance gate: photo must be at least this big AFTER
# EXIF-orient — smaller than this is either an icon or a corrupt file.
# Lowered from 300x320 after Paxton's own thumbnails came in at 224x336
# — Paxton downsizes on ingest, so the "source of truth" photos on disk
# are already small. Anything much below this is likely a favicon /
# corrupt upload, not a legit staff photo.
MIN_RESOLUTION = (200, 220)


@dataclass
class ProcessedPhoto:
    ok: bool
    reason: str = ""
    face_found: bool = False
    face_bbox: tuple[int, int, int, int] | None = None
    original_size: tuple[int, int] = (0, 0)
    output_size: tuple[int, int] = (0, 0)
    exif_rotated: bool = False
    bytes_jpeg: bytes = b""
    warnings: list[str] = field(default_factory=list)


def _detect_face(cv_gray) -> tuple[int, int, int, int] | None:
    """Return the largest face bbox (x, y, w, h) or None."""
    import cv2
    cascade_path = os.path.join(
        cv2.data.haarcascades, "haarcascade_frontalface_default.xml"
    )
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        logger.warning("photo_processor: Haar cascade failed to load")
        return None
    # scaleFactor 1.1 + minNeighbors 5 is the classic balance — fewer
    # false positives than 1.05/3 but still detects tilted faces.
    faces = cascade.detectMultiScale(
        cv_gray, scaleFactor=1.1, minNeighbors=5,
        minSize=(60, 60),
    )
    if len(faces) == 0:
        return None
    # Largest face wins — the operator usually shoots the subject up close
    largest = max(faces, key=lambda f: f[2] * f[3])
    return tuple(int(v) for v in largest)


def _crop_around_face(
    img,
    face: tuple[int, int, int, int],
) -> "Image.Image":
    """Center on the face, add headroom (roughly 0.6x face height above,
    0.5x below), then aspect-fit to TARGET_ASPECT."""
    fx, fy, fw, fh = face
    cx = fx + fw / 2
    # ID-card composition rule of thumb: eyes at ~1/3 from top. Approximate
    # by putting the face-center at 40% down the crop.
    crop_h = int(fh * 2.2)
    crop_w = int(crop_h * TARGET_ASPECT)
    cy = fy + fh / 2
    top = int(cy - crop_h * 0.4)
    left = int(cx - crop_w / 2)

    # Clamp to image bounds; if the requested crop hangs off the edge
    # (subject standing near a border), shrink until it fits rather
    # than shifting — shifting produces off-center faces.
    W, H = img.size
    if crop_w > W:
        crop_w = W
        crop_h = int(crop_w / TARGET_ASPECT)
    if crop_h > H:
        crop_h = H
        crop_w = int(crop_h * TARGET_ASPECT)
    left = max(0, min(left, W - crop_w))
    top = max(0, min(top, H - crop_h))

    return img.crop((left, top, left + crop_w, top + crop_h))


def _center_crop(img) -> "Image.Image":
    """Fallback: aspect-fit + center crop when no face is detected."""
    W, H = img.size
    target_w_over_h = TARGET_ASPECT
    src_w_over_h = W / H
    if src_w_over_h > target_w_over_h:
        # Source too wide — trim sides
        new_w = int(H * target_w_over_h)
        left = (W - new_w) // 2
        return img.crop((left, 0, left + new_w, H))
    # Source too tall — trim top+bottom evenly
    new_h = int(W / target_w_over_h)
    top = (H - new_h) // 2
    return img.crop((0, top, W, top + new_h))


def process_photo(
    raw_bytes: bytes,
    *,
    prefer_face_detection: bool = True,
) -> ProcessedPhoto:
    """Run raw photo bytes through the ID-card pipeline.

    Returns ProcessedPhoto — always with .bytes_jpeg populated when
    .ok is True. Never raises: any exception in the detector or crop
    downgrades to plain center-crop + a warning.
    """
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        return ProcessedPhoto(ok=False, reason=f"Could not decode image: {e}")

    original_size = img.size

    # ── EXIF auto-orient — the incident fix. Reads Orientation tag and
    # rotates so the image renders "right way up." No-op when EXIF is
    # missing or already 1 (top-left).
    exif_rotated = False
    try:
        orig = img
        img = ImageOps.exif_transpose(img)
        exif_rotated = img.size != orig.size or img.tobytes()[:16] != orig.tobytes()[:16]
    except Exception as e:
        logger.warning("photo_processor: EXIF orient failed: %s", e)

    # Convert to RGB — some phone photos come as RGBA (PNG) or L (scans)
    if img.mode not in ("RGB",):
        img = img.convert("RGB")

    W, H = img.size
    if W < MIN_RESOLUTION[0] or H < MIN_RESOLUTION[1]:
        return ProcessedPhoto(
            ok=False,
            reason=f"Photo too small ({W}×{H}); need ≥ {MIN_RESOLUTION[0]}×{MIN_RESOLUTION[1]}",
            original_size=original_size,
            exif_rotated=exif_rotated,
        )

    # ── Face detection
    face_bbox = None
    warnings: list[str] = []
    if prefer_face_detection:
        try:
            import cv2, numpy as np
            arr = np.array(img)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            face_bbox = _detect_face(gray)
            if not face_bbox:
                warnings.append("no_face_detected")
        except Exception as e:
            logger.warning("photo_processor: face detection failed: %s", e)
            warnings.append(f"face_detect_error:{type(e).__name__}")

    # ── Crop
    if face_bbox:
        cropped = _crop_around_face(img, face_bbox)
    else:
        cropped = _center_crop(img)

    # ── Resize to target
    cropped = cropped.resize((TARGET_W, TARGET_H), Image.LANCZOS)

    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=90, optimize=True)

    return ProcessedPhoto(
        ok=True,
        face_found=face_bbox is not None,
        face_bbox=face_bbox,
        original_size=original_size,
        output_size=cropped.size,
        exif_rotated=exif_rotated,
        bytes_jpeg=buf.getvalue(),
        warnings=warnings,
    )
