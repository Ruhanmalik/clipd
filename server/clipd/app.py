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
from pydantic import BaseModel, ValidationError

from . import db, media, storage
from .config import Config
from .ids import new_id
from .notify import human_bytes, notify_capture
from .retention import RetentionState, sweep_loop
from .slug import slugify_game

log = logging.getLogger(__name__)

DEFAULT_EXT = {"clip": ".mp4", "screenshot": ".png"}
THUMB_POSITION = 0.10  # 10% in, per plan.md §6


class IngestMeta(BaseModel):
    kind: Literal["clip", "screenshot"]
    capture_uuid: str
    source_host: str
    game: str | None = None
    game_exe: str | None = None
    title: str | None = None
    captured_at: int | None = None


def require_ingest_token(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    cfg: Config = request.app.state.cfg
    expected = f"Bearer {cfg.ingest_token}"
    # compare_digest, not ==, so a wrong token cannot be recovered by timing.
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


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

    app = FastAPI(title="clipd", lifespan=lifespan)

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
            raise HTTPException(status_code=422, detail=exc.errors()) from None

        # Idempotency: the watcher retries, and a lost response must not
        # produce a second copy. See spec §3.
        existing = db.get_by_capture_uuid(conn, parsed.capture_uuid)
        if existing:
            return {
                "id": existing.id,
                "url": f"{cfg.base_url}/v/{existing.id}",
                "duplicate": True,
            }

        clip_id = new_id()
        created_at = parsed.captured_at or int(time.time())
        game_slug = slugify_game(parsed.game)
        ext = Path(file.filename or "").suffix.lower() or DEFAULT_EXT[parsed.kind]
        rel_path = storage.rel_path_for(parsed.kind, game_slug, created_at, clip_id, ext)

        size = await storage.write_stream(cfg.data_dir / rel_path, _upload_chunks(file))

        probed = await media.probe(cfg.data_dir / rel_path)
        thumb_rel = storage.thumb_rel_path(clip_id)
        at = (probed.duration_s or 0.0) * THUMB_POSITION
        has_thumb = await media.make_thumbnail(
            cfg.data_dir / rel_path, cfg.data_dir / thumb_rel, at
        )

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
            # Two retries raced past the check above. Discard this copy and
            # return whichever one won.
            storage.remove_capture(cfg.data_dir, rel_path)
            winner = db.get_by_capture_uuid(conn, parsed.capture_uuid)
            if winner is None:
                raise
            return {
                "id": winner.id,
                "url": f"{cfg.base_url}/v/{winner.id}",
                "duplicate": True,
            }

        url = f"{cfg.base_url}/v/{clip_id}"
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
            "used_pct": round(total / budget * 100, 2) if budget else 0.0,
        }

    return app
