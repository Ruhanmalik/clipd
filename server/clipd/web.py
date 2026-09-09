"""Server-rendered pages. Spec §8, §8a.

Read-only by construction: Step 3a adds no route that writes. The mutations
(§8a, 3b) get their own module so this one stays a rendering layer.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import db, presenters

router = APIRouter()

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# Registered as globals rather than passed per-render: every page needs them,
# and a template that has to be handed its own formatters invites a caller to
# forget one and silently render a raw epoch.
templates.env.globals.update(
    display_title=presenters.display_title,
    human_duration=presenters.human_duration,
    human_bytes=presenters.human_bytes,
    relative_time=presenters.relative_time,
    pluralize=presenters.pluralize,
    UNKNOWN=presenters.UNKNOWN,
)

RECENT_LIMIT = 8


def page(request: Request, name: str, **context) -> HTMLResponse:
    """Render a template with the context every page needs.

    `now` is resolved once per request and passed down, so relative_time stays
    pure and two timestamps on the same page cannot disagree.
    """
    return templates.TemplateResponse(
        request=request, name=name, context={"now": int(time.time()), **context}
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    conn: sqlite3.Connection = request.app.state.conn
    return page(
        request,
        "index.html",
        games=db.list_games(conn),
        recent=db.recent_clips(conn, limit=RECENT_LIMIT),
    )


PAGE_SIZE = 48
KINDS = {"clip", "screenshot"}


def encode_cursor(clip: db.Clip) -> str:
    """The last row of a page, as an opaque `?before=` value."""
    return f"{clip.created_at}_{clip.id}"


def decode_cursor(raw: str | None) -> tuple[int, str] | None:
    """Parse `?before=`, or None if it is absent or malformed.

    Malformed is not an error: the value travels in a URL people paste and
    truncate, and the honest recovery is the first page, not a 400 on a page
    that renders perfectly well without a cursor.
    """
    if not raw:
        return None
    created_at, _, clip_id = raw.partition("_")
    if not created_at.isdigit() or not clip_id or "_" in clip_id:
        return None
    return int(created_at), clip_id


@router.get("/g/{game_slug}", response_class=HTMLResponse)
async def game_page(
    request: Request,
    game_slug: str,
    kind: str | None = None,
    before: str | None = None,
) -> HTMLResponse:
    conn: sqlite3.Connection = request.app.state.conn

    # An unrecognised kind is dropped rather than rejected: it can only come
    # from a hand-edited URL, and showing everything is a better answer than a
    # 422 on a page that has no invalid state of its own.
    kind = kind if kind in KINDS else None

    # The unfiltered count decides the 404: a game with captures is a real
    # page even when the current filter matches none of them.
    total_all = db.count_by_game(conn, game_slug)
    if total_all == 0:
        raise HTTPException(status_code=404, detail="not found")
    total = total_all if kind is None else db.count_by_game(
        conn, game_slug, kind=kind
    )

    # One extra row is the cheapest "is there a next page" test there is —
    # cheaper than a second COUNT with the cursor applied.
    rows = db.list_by_game(
        conn, game_slug, kind=kind,
        before=decode_cursor(before), limit=PAGE_SIZE + 1,
    )
    has_more = len(rows) > PAGE_SIZE
    clips = rows[:PAGE_SIZE]

    # The heading names the game even when the filter emptied the page, so it
    # falls back to any row rather than to the slug.
    named = clips[0] if clips else db.list_by_game(conn, game_slug, limit=1)[0]

    return page(
        request,
        "game.html",
        game_slug=game_slug,
        game_name=named.game or game_slug,
        clips=clips,
        kind=kind,
        total=total,
        next_cursor=encode_cursor(clips[-1]) if has_more and clips else None,
    )


@router.get("/v/{clip_id}", response_class=HTMLResponse)
async def clip_page(request: Request, clip_id: str) -> HTMLResponse:
    conn: sqlite3.Connection = request.app.state.conn
    clip = db.get_by_id(conn, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="not found")
    return page(request, "clip.html", clip=clip)
