"""Routes that serve bytes: the player's source, thumbnails, and downloads.

StaticFiles cannot do this job. URLs are keyed by clip id while paths are
game-sharded under data/ (spec §4), and a re-tag moves the file (spec §3), so
the mapping from URL to path is a database lookup, not a prefix.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import db, storage
from .config import Config

log = logging.getLogger(__name__)

router = APIRouter()

# Explicit, not mimetypes.guess_type: the extension reaching here came from a
# client upload, and guess_type consults the host's /etc/mime.types, which
# differs between the slim image and a dev machine. Mirrors ALLOWED_EXT in
# app.py — anything ingest accepts must be servable.
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
FALLBACK_TYPE = "application/octet-stream"

# Clip ids are never reused and a trim mints a new one, so the bytes behind
# these two routes never change under a given id — safe to cache forever.
IMMUTABLE_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


def _media_type(rel_path: str) -> str:
    return MEDIA_TYPES.get(Path(rel_path).suffix.lower(), FALLBACK_TYPE)


def _lookup(request: Request, clip_id: str) -> db.Clip:
    conn: sqlite3.Connection = request.app.state.conn
    clip = db.get_by_id(conn, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="not found")
    return clip


def _existing_path(cfg: Config, rel_path: str | None) -> Path:
    """The file behind a row, or 404.

    A row is not proof of a file: the retention sweep, a partial restore, or a
    thumbnail ffmpeg never wrote all leave the index ahead of the disk. Serving
    those as a 500 would make an ordinary gap look like an outage.
    """
    if not rel_path:
        raise HTTPException(status_code=404, detail="not found")
    try:
        path = storage.resolve_capture(cfg.data_dir, rel_path)
    except ValueError:
        # A stored path that escapes the data dir is corruption, not a request
        # problem, but it must never be served.
        log.error("refusing to serve out-of-store path %r", rel_path)
        raise HTTPException(status_code=404, detail="not found") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return path


@router.get("/m/{clip_id}")
async def media(request: Request, clip_id: str) -> FileResponse:
    """The player's source. Inline, and Range-capable so seeking works.

    Starlette's FileResponse implements Range itself, including 206 and
    multi-range. Nothing here needs to parse the header.
    """
    clip = _lookup(request, clip_id)
    path = _existing_path(request.app.state.cfg, clip.rel_path)
    return FileResponse(
        path, media_type=_media_type(clip.rel_path), headers=IMMUTABLE_CACHE
    )


@router.get("/t/{clip_id}")
async def thumbnail(request: Request, clip_id: str) -> FileResponse:
    clip = _lookup(request, clip_id)
    path = _existing_path(request.app.state.cfg, clip.thumb_path)
    return FileResponse(path, media_type="image/jpeg", headers=IMMUTABLE_CACHE)


@router.get("/d/{clip_id}")
async def download(request: Request, clip_id: str) -> FileResponse:
    """The original bytes, as a save-file rather than a stream.

    The filename is built from game_slug, which slug.py has already reduced to
    [a-z0-9-] — so it needs no escaping in the Content-Disposition header, and
    it lands in the user's downloads folder with a name that says what it is.
    """
    clip = _lookup(request, clip_id)
    path = _existing_path(request.app.state.cfg, clip.rel_path)
    suffix = Path(clip.rel_path).suffix
    return FileResponse(
        path,
        media_type=_media_type(clip.rel_path),
        filename=f"{clip.game_slug}-{clip.id}{suffix}",
    )
