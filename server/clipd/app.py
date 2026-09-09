"""FastAPI application: routes, auth, and lifespan wiring."""
from __future__ import annotations

import asyncio
import hmac
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError, field_validator

from . import db, files, media, storage, web
from .config import Config
from .ids import new_id
from .notify import human_bytes, notify_capture
from .retention import RetentionState, sweep_loop
from .slug import slugify_game

log = logging.getLogger(__name__)

DEFAULT_EXT = {"clip": ".mp4", "screenshot": ".png"}
THUMB_POSITION = 0.10  # 10% in, per plan.md §6

# The stored extension is client-controlled, and Step 3 will serve these paths
# (and plan.md §7 may expose them through a tunnel). An arbitrary extension
# means a stored .html served as text/html, or ENAMETOOLONG on a long one.
# Mirrors KIND_FOR_SUFFIX in client/clipwatch/remux.py. This set is the wider
# of the two; the client only ever uploads a subset.
ALLOWED_EXT = {
    "clip": {".mp4", ".mkv", ".mov", ".webm"},
    "screenshot": {".png", ".jpg", ".jpeg", ".webp"},
}

# Sanity bounds on the client's clock. A fresh Windows install before NTP
# reports 1970, which would file captures in the past and make them the first
# thing the oldest-first sweep deletes.
MIN_CAPTURED_AT = 946_684_800   # 2000-01-01
CLOCK_SKEW_S = 86_400           # tolerate a day ahead


class IngestMeta(BaseModel):
    kind: Literal["clip", "screenshot"]
    capture_uuid: str
    source_host: str
    game: str | None = None
    game_exe: str | None = None
    title: str | None = None
    captured_at: int | None = None

    @field_validator("captured_at")
    @classmethod
    def _plausible_clock(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not MIN_CAPTURED_AT <= value <= int(time.time()) + CLOCK_SKEW_S:
            raise ValueError("captured_at is outside the plausible range")
        return value


def require_ingest_token(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    cfg: Config = request.app.state.cfg
    expected = f"Bearer {cfg.ingest_token}"
    # compare_digest, not ==, so a wrong token cannot be recovered by timing.
    # Compare bytes: Starlette decodes headers as latin-1, and compare_digest
    # raises TypeError on a non-ASCII str, which would 500 on unauthenticated
    # input instead of returning 401.
    if not authorization or not hmac.compare_digest(
        authorization.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


def _capture_ext(filename: str | None, kind: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in ALLOWED_EXT[kind] else DEFAULT_EXT[kind]


def _clip_url(cfg: Config, clip_id: str) -> str:
    """The one definition of a clip's URL. /v/<id> is a step 3 route."""
    return f"{cfg.base_url}/v/{clip_id}"


def _duplicate_response(cfg: Config, clip: db.Clip) -> dict:
    return {"id": clip.id, "url": _clip_url(cfg, clip.id), "duplicate": True}


def _discard(cfg: Config, rel_path: str, thumb_rel: str) -> None:
    """Drop a capture and its thumbnail. Best-effort: the caller is unwinding."""
    for rel in (rel_path, thumb_rel):
        try:
            storage.remove_capture(cfg.data_dir, rel)
        except Exception:
            log.warning("could not discard %s", rel, exc_info=True)


async def _upload_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    """Yield the upload in 1 MiB pieces. Never materialize the whole clip."""
    while chunk := await file.read(storage.CHUNK):
        yield chunk


def create_app(cfg: Config) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.cfg = cfg
        app.state.conn = db.connect(cfg.data_dir / "clipd.db")
        db.init_schema(app.state.conn)

        task = None
        # SWEEP_INTERVAL_S=0 disables the sweep. Tests rely on this.
        if cfg.sweep_interval_s > 0:
            task = asyncio.create_task(
                sweep_loop(app.state.conn, cfg, RetentionState())
            )

        yield

        if task is not None:
            task.cancel()
        app.state.conn.close()

    # No interactive docs: they are unauthenticated and advertise the /ingest
    # contract, and they are not in the spec's route table.
    app = FastAPI(
        title="clipd",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def enforce_upload_cap(request: Request, call_next):
        """Reject oversized bodies before the multipart parser spools them.

        FastAPI resolves path dependencies *after* parsing the form, so the
        bearer check cannot gate this: without the cap, any tailnet host could
        spool an unbounded body to the container's writable layer, which is
        outside the bind mount and so outside MAX_STORE_BYTES entirely.
        """
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cfg.max_upload_bytes:
            return JSONResponse({"detail": "upload too large"}, status_code=413)
        return await call_next(request)


    @app.post("/ingest", dependencies=[Depends(require_ingest_token)])
    async def ingest(
        request: Request,
        file: UploadFile = File(...),
        meta: str = Form(...),
    ) -> dict:
        conn: sqlite3.Connection = request.app.state.conn

        try:
            parsed = IngestMeta.model_validate_json(meta)
        except ValidationError as exc:
            # include_context=False: pydantic puts the live exception object in
            # ctx, which is not JSON-serializable, so rendering the 422 would
            # itself 500.
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_context=False, include_url=False),
            ) from None

        # Idempotency: the watcher retries, and a lost response must not
        # produce a second copy. See spec §3.
        existing = db.get_by_capture_uuid(conn, parsed.capture_uuid)
        if existing:
            return _duplicate_response(cfg, existing)

        clip_id = new_id()
        created_at = parsed.captured_at or int(time.time())
        game_slug = slugify_game(parsed.game)
        ext = _capture_ext(file.filename, parsed.kind)
        rel_path = storage.rel_path_for(parsed.kind, game_slug, created_at, clip_id, ext)
        thumb_rel = storage.thumb_rel_path(clip_id)

        size = await storage.write_stream(cfg.data_dir / rel_path, _upload_chunks(file))

        # Everything from here to the insert must clean up after itself. A file
        # on disk with no row is invisible to the retention sweep, which sums
        # `bytes` from the index — and plan.md §8.7 has the watcher retrying
        # until it gets a 200, so one fault becomes an orphan per retry.
        try:
            probed = await media.probe(cfg.data_dir / rel_path)
            at = (probed.duration_s or 0.0) * THUMB_POSITION
            has_thumb = await media.make_thumbnail(
                cfg.data_dir / rel_path, cfg.data_dir / thumb_rel, at
            )
        except BaseException:
            _discard(cfg, rel_path, thumb_rel)
            raise

        clip = db.Clip(
            id=clip_id,
            public_slug=None,
            capture_uuid=parsed.capture_uuid,
            kind=parsed.kind,
            title=parsed.title,
            filename=f"{clip_id}{ext}",
            rel_path=rel_path,
            bytes=size,
            duration_s=probed.duration_s,
            width=probed.width,
            height=probed.height,
            game=parsed.game,
            game_slug=game_slug,
            game_exe=parsed.game_exe,
            pinned=0,
            source_host=parsed.source_host,
            created_at=created_at,
            thumb_path=thumb_rel if has_thumb else None,
        )

        try:
            db.insert_clip(conn, clip)
        except sqlite3.IntegrityError:
            # Two retries raced past the check above. Discard this copy —
            # thumbnail included, or it outlives the row that referenced it —
            # and return whichever one won.
            _discard(cfg, rel_path, thumb_rel)
            winner = db.get_by_capture_uuid(conn, parsed.capture_uuid)
            if winner is None:
                raise
            return _duplicate_response(cfg, winner)

        url = _clip_url(cfg, clip_id)
        duration = f"{probed.duration_s:.0f}s · " if probed.duration_s else ""
        await notify_capture(
            cfg,
            title=parsed.game or "Unknown",
            body=f"{duration}{human_bytes(size)}",
            click_url=url,
        )

        log.info("ingested %s (%s, %s) from %s",
                 clip_id, game_slug, human_bytes(size), parsed.source_host)
        return {"id": clip_id, "url": url}

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        conn = request.app.state.conn
        count, total = db.store_totals(conn)
        budget = cfg.max_store_bytes
        return {
            "status": "ok",
            "clips": count,
            "bytes": total,
            "budget_bytes": budget,
            # budget is >= MIN_STORE_BYTES; config refuses 0.
            "used_pct": round(total / budget * 100, 2),
        }

    app.mount(
        "/static",
        StaticFiles(directory=web.TEMPLATE_DIR.parent / "static"),
        name="static",
    )
    app.include_router(files.router)
    app.include_router(web.router)

    return app
