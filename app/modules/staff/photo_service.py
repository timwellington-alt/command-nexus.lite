"""Staff photo library — single-source CRUD service.

Nexus is the system of record for staff photos. Files live on disk
under MEDIA_DIR; metadata (uploaded_by, uploaded_at, is_active)
lives in the ``staff_photos`` table. The two are kept in sync by
this service — nothing else should write to either.

If a district later adds Paxton (or Active Directory, or anything
else that wants to consume photos), they can read from here. Nexus
never pulls photos from an external source into the gallery — the
"paxton as fallback library entry" pattern that lived in the full
nexus codebase is gone.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MEDIA_DIR = os.environ.get("MEDIA_DIR", "data/photos")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_IMAGE_DIM = 400  # px — resize so neither side exceeds this


def is_valid_image(content: bytes) -> bool:
    """Reject anything that doesn't actually decode as an image."""
    try:
        from PIL import Image as PILImage
        PILImage.open(BytesIO(content)).verify()
        return True
    except Exception:
        return False


def _normalize_jpeg(content: bytes) -> bytes:
    """Decode -> RGB -> thumbnail -> JPEG. Returns the normalized bytes."""
    from PIL import Image as PILImage
    img = PILImage.open(BytesIO(content)).convert("RGB")
    img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), PILImage.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _new_filename(username: str) -> str:
    """Opaque per-upload filename — timestamp + random suffix.

    Historically Nexus used ``profile_{username}_{ts}.jpg`` which was
    readable but let anyone with staff.view enumerate photos by
    guessing timestamps. UUID suffix makes that infeasible while
    still being sortable (same-prefix lexicographic ordering ≈
    chronological).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand = uuid.uuid4().hex[:8]
    safe_user = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in username)
    return f"profile_{safe_user}_{ts}_{rand}.jpg"


def _safe_user(username: str) -> str:
    """Sanitize a username for use in a filename (same rule as _new_filename)."""
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in username)


def _active_marker_path(username: str) -> str:
    """Legacy sync-callable active-photo marker. Dual-written alongside
    the DB row so synchronous callers (staff list hot path) can resolve
    an active photo without an async DB query."""
    return os.path.join(MEDIA_DIR, f"profile_{_safe_user(username)}_active.jpg")


async def upload_photo(
    db: AsyncSession,
    *,
    username: str,
    raw_bytes: bytes,
    uploaded_by: str,
    source: str = "upload",
    set_active: bool = True,
) -> dict[str, Any]:
    """Upload a new photo for a staff member.

    - Normalizes the input (decode, convert to JPEG, resize)
    - Writes bytes to disk under MEDIA_DIR
    - Inserts a staff_photos row
    - If set_active=True, clears any other active photo for this
      staff member first (enforced by the partial unique index)
    """
    if len(raw_bytes) > MAX_UPLOAD_SIZE:
        raise ValueError(f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")
    if not is_valid_image(raw_bytes):
        raise ValueError("Invalid image content")

    jpeg = _normalize_jpeg(raw_bytes)
    filename = _new_filename(username)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    with open(os.path.join(MEDIA_DIR, filename), "wb") as f:
        f.write(jpeg)

    if set_active:
        await db.execute(
            text("UPDATE staff_photos SET is_active = false "
                 "WHERE lower(staff_username) = lower(:u) AND is_active"),
            {"u": username},
        )
        # Dual-write the legacy sync-callable marker.
        with open(_active_marker_path(username), "wb") as f:
            f.write(jpeg)

    row = (await db.execute(
        text("""
            INSERT INTO staff_photos
                (staff_username, filename, is_active, uploaded_by, source, bytes)
            VALUES (:u, :f, :a, :by, :src, :sz)
            RETURNING id
        """),
        {"u": username, "f": filename, "a": set_active,
         "by": uploaded_by, "src": source, "sz": len(jpeg)},
    )).first()
    return {"id": row[0], "filename": filename, "bytes": jpeg}


async def list_photos(db: AsyncSession, *, username: str) -> list[dict[str, Any]]:
    """List all photos for a staff member, newest first."""
    rows = (await db.execute(
        text("""
            SELECT id, filename, is_active, uploaded_by, uploaded_at, source, bytes
            FROM staff_photos
            WHERE lower(staff_username) = lower(:u)
            ORDER BY uploaded_at DESC
        """),
        {"u": username},
    )).mappings().all()
    return [dict(r) for r in rows]


async def set_active(db: AsyncSession, *, username: str, photo_id: int) -> dict[str, Any] | None:
    """Flip the active flag to this photo (and off everyone else's).

    Returns the activated row dict, or None if photo_id doesn't belong
    to this user."""
    row = (await db.execute(
        text("""
            SELECT id, filename FROM staff_photos
            WHERE id = :pid AND lower(staff_username) = lower(:u)
        """),
        {"pid": photo_id, "u": username},
    )).first()
    if not row:
        return None

    await db.execute(
        text("UPDATE staff_photos SET is_active = false "
             "WHERE lower(staff_username) = lower(:u) AND is_active"),
        {"u": username},
    )
    await db.execute(
        text("UPDATE staff_photos SET is_active = true WHERE id = :pid"),
        {"pid": photo_id},
    )
    # Refresh the legacy sync marker so staff-list sync lookups see
    # the newly-active photo.
    src = os.path.join(MEDIA_DIR, row[1])
    if os.path.isfile(src):
        import shutil
        shutil.copy2(src, _active_marker_path(username))
    return {"id": row[0], "filename": row[1]}


async def delete_photo(
    db: AsyncSession,
    *,
    username: str,
    photo_id: int,
    actor_email: str,
    actor_is_admin: bool,
) -> bool:
    """Delete a photo row + unlink the file on disk.

    Permission rules (per Tim's directive):
      - Users may delete photos they themselves uploaded (uploaded_by
        matches their email).
      - Admins may delete any photo.
    Returns False if the photo doesn't exist OR the actor lacks
    permission to delete it."""
    row = (await db.execute(
        text("""
            SELECT id, filename, uploaded_by, is_active
            FROM staff_photos
            WHERE id = :pid AND lower(staff_username) = lower(:u)
        """),
        {"pid": photo_id, "u": username},
    )).first()
    if not row:
        return False

    _, filename, uploaded_by, _is_active = row
    if not actor_is_admin and (uploaded_by or "").lower() != (actor_email or "").lower():
        return False

    await db.execute(text("DELETE FROM staff_photos WHERE id = :pid"), {"pid": photo_id})

    # Unlink file — best-effort. If the row was already deleted but
    # the file lingers from a crashed earlier run, that's fine.
    try:
        os.unlink(os.path.join(MEDIA_DIR, filename))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("staff_photos: failed to unlink %s: %s", filename, e)

    # If we just deleted the active photo, drop the legacy marker and
    # try to promote the newest remaining photo to active.
    if _is_active:
        try:
            os.unlink(_active_marker_path(username))
        except FileNotFoundError:
            pass
        next_row = (await db.execute(
            text("""
                SELECT id, filename FROM staff_photos
                WHERE lower(staff_username) = lower(:u)
                ORDER BY uploaded_at DESC LIMIT 1
            """),
            {"u": username},
        )).first()
        if next_row:
            await db.execute(
                text("UPDATE staff_photos SET is_active = true WHERE id = :pid"),
                {"pid": next_row[0]},
            )
            src = os.path.join(MEDIA_DIR, next_row[1])
            if os.path.isfile(src):
                import shutil
                shutil.copy2(src, _active_marker_path(username))

    return True


async def resolve_active_filename(db: AsyncSession, *, username: str) -> str | None:
    """Return the on-disk filename of the active photo, or None."""
    row = (await db.execute(
        text("""
            SELECT filename FROM staff_photos
            WHERE lower(staff_username) = lower(:u) AND is_active
            LIMIT 1
        """),
        {"u": username},
    )).first()
    return row[0] if row else None


def photo_cache_buster(filename: str) -> str:
    """Return a ``?v=<mtime>`` suffix so browsers don't serve stale
    photos after an upload rotation. Mirrors the behavior the
    filesystem-only version provided."""
    try:
        m = int(os.path.getmtime(os.path.join(MEDIA_DIR, filename)) * 1000)
        return f"?v={m}"
    except Exception:
        return ""
