"""Server-rendered pages. Spec §8, §8a.

Read-only by construction: Step 3a adds no route that writes. The mutations
(§8a, 3b) get their own module so this one stays a rendering layer.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Request
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
