# clipd Web UI — Step 3a (read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the clip store browsable — a game-first landing page, a per-game listing, and a detail page with a working, seekable player — so the URL the watcher puts on the clipboard finally opens something.

**Architecture:** Server-rendered Jinja2 pages over the SQLite index built in Step 1. No SPA, no build step, no client-side router. Three new modules keep `app.py` as wiring rather than letting it grow a UI: `presenters.py` holds pure display helpers, `web.py` holds the three HTML routes, `files.py` holds the three binary routes. All six are registered as `APIRouter`s. Read-side queries live in `db.py` beside the existing ones.

**Tech Stack:** Python 3.12, FastAPI, Jinja2 3.1, stdlib `sqlite3`, Starlette `FileResponse`, vanilla CSS and JS. No frontend framework, no CSS framework, no bundler.

**Spec:** `docs/superpowers/specs/2026-09-07-clipd-design.md` — §6 (API surface), §8 (web app), and §8a (Step 3 scope decisions, which supersede §8 where they differ).

## Global Constraints

Every task's requirements implicitly include this section.

- Python **3.12**, matching the rest of the repo.
- **This is Step 3a: read-only.** No route added here may modify the database or the filesystem. `PATCH`, `DELETE`, bulk-delete, trim, share, and `/c/<slug>` are Step 3b (§8a) — do not build them, and do not add "just the endpoint" for a later task.
- **No application-level auth on any route** (§8a, Authentication). Tailscale is the entire boundary. Do not add a token check, a session, or a login.
- **The server never transcodes** (`plan.md` §5). 3a shells out to no media tooling at all — not even `ffprobe`. Every dimension, duration, and thumbnail already sits in the index from ingest.
- **Serve files only through `storage.resolve_capture`**, never by joining `data_dir` with a path from the request. `Path("/data") / "/etc/passwd"` is `/etc/passwd`.
- **Presentation logic is pure and lives in `presenters.py`**, not in templates and not in route handlers. A Jinja template must not compute; it interpolates.
- **Templates and static assets must be declared as package data.** The Dockerfile installs with `pip install .` (`server/Dockerfile:13`), so anything not declared is absent from the image and the container 500s on its first page render while passing every local test.
- Match the house comment style: comment the **why** of a load-bearing decision, never the what. `db.py` and `storage.py` are the reference.
- `pytest` must pass from `server/` with no network access and no `ffmpeg` invocation.

## Prerequisite

None. Everything 3a needs exists: the schema (§3), the storage layout (§4), and `Config` are all Step 1 deliverables already on `main`.

## File structure

```
server/
├── pyproject.toml                 # MODIFY: jinja2 dep + package-data declaration
├── Dockerfile                     # unchanged — COPY clipd already carries the new dirs
└── clipd/
    ├── app.py                     # MODIFY: include the two routers
    ├── db.py                      # MODIFY: read-side queries (GameSummary, list_games, ...)
    ├── storage.py                 # MODIFY: promote _resolve_inside to resolve_capture
    ├── presenters.py              # NEW: pure display helpers
    ├── web.py                     # NEW: GET /, /g/<slug>, /v/<id>
    ├── files.py                   # NEW: GET /m/<id>, /t/<id>, /d/<id>
    ├── templates/
    │   ├── base.html              # NEW: shell, nav, <head>
    │   ├── index.html             # NEW: recent strip + game tiles
    │   ├── game.html              # NEW: one game, kind filter, pagination
    │   └── clip.html              # NEW: player + metadata
    └── static/
        ├── app.css                # NEW: the entire stylesheet
        └── app.js                 # NEW: keyboard shortcuts only
```

Why three modules rather than adding to `app.py`: `app.py` is already 263 lines carrying the ingest pipeline and its unwinding logic. Six more routes plus template wiring would roughly double it and mix two unrelated concerns in one file. `web.py` and `files.py` split by responsibility — HTML versus bytes — not by technical layer, and each is independently readable.

---

### Task 1: Read-side queries

`db.py` can currently fetch one clip by id or by capture_uuid, and can enumerate rows for the retention sweep. None of that shape serves a gallery. This task adds the four queries the pages need.

**Files:**
- Modify: `server/clipd/db.py` (append after `get_by_capture_uuid`, line 99)
- Modify: `server/tests/conftest.py` (add the shared `make_clip` factory)
- Test: `server/tests/test_queries.py`

**Interfaces:**
- Consumes: `db.Clip`, `db._to_clip`, `db.connect`, `db.init_schema` (all existing)
- Produces:
  - `GameSummary` frozen dataclass: `game_slug: str`, `game: str | None`, `clips: int`, `bytes: int`, `latest_at: int`, `cover_id: str | None`
  - `list_games(conn: sqlite3.Connection) -> list[GameSummary]`
  - `recent_clips(conn: sqlite3.Connection, limit: int = 8) -> list[Clip]`
  - `list_by_game(conn, game_slug: str, *, kind: str | None = None, before: tuple[int, str] | None = None, limit: int = 48) -> list[Clip]`
  - `count_by_game(conn, game_slug: str, *, kind: str | None = None) -> int`

- [ ] **Step 1: Add the shared clip factory**

Every new test module needs to build `db.Clip` rows. A module-level helper would
have to be imported across test files, which pytest's default prepend import mode
does not make reliable. A factory fixture needs no import at all.

Append to `server/tests/conftest.py`:

```python
from clipd import db


@pytest.fixture
def make_clip():
    """Build a db.Clip row. Any column can be overridden by keyword."""
    def _make(clip_id="aB3xY9z", *, created_at=1757260800, game="Halo",
              game_slug="halo", kind="clip", size=1000, thumb=True, **overrides):
        row = dict(
            id=clip_id, public_slug=None, capture_uuid=f"u-{clip_id}", kind=kind,
            title=None, filename=f"{clip_id}.mp4",
            rel_path=f"clips/{game_slug}/2026/09/{clip_id}.mp4", bytes=size,
            duration_s=90.0, width=1920, height=1080, game=game,
            game_slug=game_slug, game_exe="halo.exe", pinned=0,
            source_host="desktop-amtr56i", created_at=created_at,
            thumb_path=f"thumbs/{clip_id}.jpg" if thumb else None,
        )
        row.update(overrides)
        return db.Clip(**row)
    return _make
```

- [ ] **Step 2: Write the failing test**

```python
# server/tests/test_queries.py
"""Read-side queries behind the gallery. Step 3a, spec §8/§8a."""
import pytest
from clipd import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()


def add(conn, *clips):
    for clip in clips:
        db.insert_clip(conn, clip)


def test_list_games_groups_and_counts(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100),
        make_clip("b", created_at=200),
        make_clip("c", created_at=150, game="Counter-Strike 2",
                  game_slug="counter-strike-2"))

    games = {g.game_slug: g for g in db.list_games(conn)}

    assert games["halo"].clips == 2
    assert games["halo"].bytes == 2000
    assert games["halo"].latest_at == 200
    assert games["counter-strike-2"].clips == 1


def test_list_games_orders_most_recent_first(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100, game="Halo", game_slug="halo"),
        make_clip("b", created_at=300, game="Doom", game_slug="doom"),
        make_clip("c", created_at=200, game="Myst", game_slug="myst"))

    assert [g.game_slug for g in db.list_games(conn)] == ["doom", "myst", "halo"]


def test_list_games_covers_with_the_newest_clip(conn, make_clip):
    add(conn,
        make_clip("old", created_at=100),
        make_clip("new", created_at=900))

    (halo,) = db.list_games(conn)
    assert halo.cover_id == "new"


def test_list_games_is_empty_on_an_empty_store(conn):
    assert db.list_games(conn) == []


def test_recent_clips_returns_newest_first_and_respects_limit(conn, make_clip):
    add(conn, *[make_clip(f"c{i}", created_at=i) for i in range(10)])

    recent = db.recent_clips(conn, limit=3)

    assert [c.id for c in recent] == ["c9", "c8", "c7"]


def test_list_by_game_filters_to_one_game(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100),
        make_clip("b", created_at=200, game="Doom", game_slug="doom"))

    assert [c.id for c in db.list_by_game(conn, "halo")] == ["a"]


def test_list_by_game_filters_by_kind(conn, make_clip):
    add(conn,
        make_clip("clip1", created_at=100),
        make_clip("shot1", created_at=200, kind="screenshot"))

    assert [c.id for c in db.list_by_game(conn, "halo", kind="screenshot")] == ["shot1"]
    assert [c.id for c in db.list_by_game(conn, "halo", kind="clip")] == ["clip1"]


def test_list_by_game_paginates_by_keyset(conn, make_clip):
    add(conn, *[make_clip(f"c{i}", created_at=i) for i in range(5)])

    first = db.list_by_game(conn, "halo", limit=2)
    assert [c.id for c in first] == ["c4", "c3"]

    cursor = (first[-1].created_at, first[-1].id)
    second = db.list_by_game(conn, "halo", limit=2, before=cursor)
    assert [c.id for c in second] == ["c2", "c1"]


def test_pagination_advances_when_two_captures_share_a_second(conn, make_clip):
    """A whole page at one timestamp must not stall the cursor.

    Comparing created_at alone would return the same rows forever, because
    every row on the next page also satisfies created_at <= the cursor.
    """
    add(conn, *[make_clip(f"c{i}", created_at=500) for i in range(4)])

    first = db.list_by_game(conn, "halo", limit=2)
    cursor = (first[-1].created_at, first[-1].id)
    second = db.list_by_game(conn, "halo", limit=2, before=cursor)

    assert {c.id for c in first} & {c.id for c in second} == set()
    assert len(second) == 2


def test_count_by_game_counts_with_and_without_a_kind(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100),
        make_clip("b", created_at=200),
        make_clip("s", created_at=300, kind="screenshot"))

    assert db.count_by_game(conn, "halo") == 3
    assert db.count_by_game(conn, "halo", kind="clip") == 2
    assert db.count_by_game(conn, "doom") == 0
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd server && python -m pytest tests/test_queries.py -v`
Expected: FAIL — `AttributeError: module 'clipd.db' has no attribute 'list_games'`

- [ ] **Step 4: Implement the queries**

Append to `server/clipd/db.py`, after `get_by_capture_uuid` (line 99):

```python
@dataclass(frozen=True)
class GameSummary:
    """One tile on the landing page."""
    game_slug: str
    game: str | None
    clips: int
    bytes: int
    latest_at: int
    cover_id: str | None


def _to_clips(cur: sqlite3.Cursor) -> list[Clip]:
    return [c for c in (_to_clip(r) for r in cur.fetchall()) if c is not None]


def list_games(conn: sqlite3.Connection) -> list[GameSummary]:
    """One row per game, newest activity first.

    `game` and `id` are bare columns beside MAX(created_at). SQLite documents
    these as taking their values from the row that produced the maximum, which
    is exactly the cover we want — the newest capture — without a correlated
    subquery per game.
    """
    cur = conn.execute("""
        SELECT game_slug,
               game,
               id                       AS cover_id,
               COUNT(*)                 AS clips,
               COALESCE(SUM(bytes), 0)  AS total_bytes,
               MAX(created_at)          AS latest_at
        FROM clips
        GROUP BY game_slug
        ORDER BY latest_at DESC
    """)
    return [
        GameSummary(
            game_slug=row["game_slug"],
            game=row["game"],
            clips=int(row["clips"]),
            bytes=int(row["total_bytes"]),
            latest_at=int(row["latest_at"]),
            cover_id=row["cover_id"],
        )
        for row in cur.fetchall()
    ]


def recent_clips(conn: sqlite3.Connection, limit: int = 8) -> list[Clip]:
    """The newest captures across every game — the landing page's Recent strip."""
    cur = conn.execute(
        "SELECT * FROM clips ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return _to_clips(cur)


def _game_filter(game_slug: str, kind: str | None) -> tuple[list[str], list]:
    where = ["game_slug = ?"]
    params: list = [game_slug]
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    return where, params


def list_by_game(
    conn: sqlite3.Connection,
    game_slug: str,
    *,
    kind: str | None = None,
    before: tuple[int, str] | None = None,
    limit: int = 48,
) -> list[Clip]:
    """One page of a game's captures, newest first.

    Keyset, not OFFSET: the store is capped by bytes, not rows, so a deep page
    on a large game would make SQLite walk every skipped row.

    `before` is the (created_at, id) of the last row on the previous page. The
    comparison is a row value rather than created_at alone, because captures
    taken in the same second are common — a burst of screenshots — and a page
    boundary landing inside one would otherwise return the same rows forever.
    """
    where, params = _game_filter(game_slug, kind)
    if before is not None:
        where.append("(created_at, id) < (?, ?)")
        params.extend(before)
    params.append(limit)

    cur = conn.execute(
        f"SELECT * FROM clips WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    )
    return _to_clips(cur)


def count_by_game(
    conn: sqlite3.Connection, game_slug: str, *, kind: str | None = None
) -> int:
    where, params = _game_filter(game_slug, kind)
    cur = conn.execute(
        f"SELECT COUNT(*) FROM clips WHERE {' AND '.join(where)}", params
    )
    return int(cur.fetchone()[0])
```

Also update the existing `prunable_oldest_first` to use the new helper, since it now duplicates it:

```python
def prunable_oldest_first(conn: sqlite3.Connection) -> list[Clip]:
    cur = conn.execute(
        f"SELECT * FROM clips WHERE NOT {_PROTECTED} ORDER BY created_at ASC"
    )
    return _to_clips(cur)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server && python -m pytest tests/test_queries.py tests/test_db.py tests/test_retention.py -v`
Expected: PASS — including the pre-existing db and retention tests, which must not regress from the `_to_clips` refactor.

- [ ] **Step 6: Commit and push**

```bash
git add server/clipd/db.py server/tests/conftest.py server/tests/test_queries.py
git commit -m "feat: add read-side queries for the gallery"
git push origin feat/clipd-web-ui
```

---

### Task 2: Display helpers

Every value the pages show needs shaping: a duration in seconds is not "1:30", an epoch is not "2 hours ago", and a clip with no custom title needs one derived. Doing that in Jinja spreads logic into templates where it cannot be unit tested; doing it in route handlers duplicates it across three routes. It goes in one pure module.

**Files:**
- Create: `server/clipd/presenters.py`
- Test: `server/tests/test_presenters.py`

**Interfaces:**
- Consumes: `db.Clip`, and `notify.human_bytes` (existing, `server/clipd/notify.py:14`)
- Produces:
  - `display_title(clip: Clip) -> str`
  - `human_duration(seconds: float | None) -> str`
  - `relative_time(then: int, now: int) -> str`
  - `human_bytes` re-exported, so templates have one import site

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_presenters.py
"""Pure display helpers. No clock, no database, no I/O."""
import pytest
from clipd import presenters


@pytest.mark.parametrize("seconds,expected", [
    (None, ""),
    (0, ""),
    (7.4, "0:07"),
    (60, "1:00"),
    (90.6, "1:30"),
    (599, "9:59"),
    (3600, "1:00:00"),
    (3725, "1:02:05"),
])
def test_human_duration(seconds, expected):
    assert presenters.human_duration(seconds) == expected


def test_display_title_prefers_a_custom_title(make_clip):
    assert presenters.display_title(make_clip()) == "Halo"
    assert presenters.display_title(make_clip(title="the 1v5")) == "the 1v5"


def test_display_title_falls_back_to_unknown_when_the_game_is_missing(make_clip):
    clip = make_clip("a", created_at=1757260800, game=None)
    assert presenters.display_title(clip) == "Unknown"


@pytest.mark.parametrize("delta,expected", [
    (0, "just now"),
    (45, "just now"),
    (60, "1 minute ago"),
    (120, "2 minutes ago"),
    (3600, "1 hour ago"),
    (7200, "2 hours ago"),
    (86400, "yesterday"),
    (172800, "2 days ago"),
    (2592000, "30 days ago"),
])
def test_relative_time(delta, expected):
    now = 1757260800
    assert presenters.relative_time(now - delta, now) == expected


def test_relative_time_does_not_report_the_future_as_ago():
    """A capture whose clock ran ahead must not read '-3 minutes ago'.

    /ingest tolerates a day of skew (app.py CLOCK_SKEW_S), so rows genuinely
    can carry a created_at in the future.
    """
    now = 1757260800
    assert presenters.relative_time(now + 300, now) == "just now"


def test_human_bytes_is_reexported():
    assert presenters.human_bytes(1024) == "1.0 KB"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd server && python -m pytest tests/test_presenters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.presenters'`

- [ ] **Step 3: Implement the helpers**

```python
# server/clipd/presenters.py
"""Pure display helpers for the templates.

Kept out of both the templates and the route handlers: a Jinja template cannot
be unit tested, and three routes need the same shaping. Nothing here touches
the clock, the database, or the disk — `now` is always passed in.
"""
from __future__ import annotations

from .db import Clip
from .notify import human_bytes

# Re-exported so templates have a single import site for value formatting.
__all__ = ["display_title", "human_duration", "relative_time", "human_bytes"]

UNKNOWN = "Unknown"

MINUTE = 60
HOUR = 3600
DAY = 86400


def display_title(clip: Clip) -> str:
    """The clip's own title, else its game, else Unknown.

    Unknown matches what the watcher already sends for an unresolved game
    (spec §5), so a re-tagging-queue tile reads the same everywhere.
    """
    return clip.title or clip.game or UNKNOWN


def human_duration(seconds: float | None) -> str:
    """m:ss, or h:mm:ss past an hour. Empty for a screenshot."""
    if not seconds or seconds <= 0:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, HOUR)
    minutes, secs = divmod(remainder, MINUTE)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit} ago" if count == 1 else f"{count} {unit}s ago"


def relative_time(then: int, now: int) -> str:
    """Coarse age. Clamped at zero: /ingest accepts a day of clock skew, so a
    row's created_at can genuinely sit in the future, and "-3 minutes ago"
    would be the visible result."""
    delta = max(0, now - then)

    if delta < MINUTE:
        return "just now"
    if delta < HOUR:
        return _plural(delta // MINUTE, "minute")
    if delta < DAY:
        return _plural(delta // HOUR, "hour")
    if delta < 2 * DAY:
        return "yesterday"
    return _plural(delta // DAY, "day")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd server && python -m pytest tests/test_presenters.py -v`
Expected: PASS

- [ ] **Step 5: Commit and push**

```bash
git add server/clipd/presenters.py server/tests/test_presenters.py
git commit -m "feat: add pure display helpers for the gallery"
git push origin feat/clipd-web-ui
```

---
### Task 3: Binary routes — media, thumbnail, download

The player needs a URL it can seek in. `/d/<id>` is `Content-Disposition: attachment` (§6) and so cannot double as a `<video>` source, and `StaticFiles` cannot serve either: URLs are keyed by `id` while paths are game-sharded, and a 3b re-tag moves the file (§8a).

**Starlette 1.6.0's `FileResponse` already implements Range**, including `206 Partial Content`, `Content-Range`, and multi-range requests — verified in the installed version. Do **not** hand-roll range parsing; passing the path to `FileResponse` is the whole implementation.

**Files:**
- Create: `server/clipd/files.py`
- Modify: `server/clipd/storage.py:47` (rename `_resolve_inside` → `resolve_capture`, update its two callers at lines 74-75 and 82)
- Modify: `server/clipd/app.py` (include the router)
- Test: `server/tests/test_files.py`

**Interfaces:**
- Consumes: `db.get_by_id`, `storage.resolve_capture`, `request.app.state.cfg`, `request.app.state.conn`
- Produces:
  - `storage.resolve_capture(data_dir: Path, rel_path: str) -> Path`
  - `files.router: APIRouter` serving `GET /m/{clip_id}`, `GET /t/{clip_id}`, `GET /d/{clip_id}`
  - `files.MEDIA_TYPES: dict[str, str]`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_files.py
"""The three binary routes: inline media, thumbnail, download."""
import pytest
from clipd import db



@pytest.fixture
def stored(client, cfg, make_clip):
    """One clip whose bytes and thumbnail actually exist on disk."""
    clip = make_clip("aB3xY9z", created_at=1757260800)
    db.insert_clip(client.app.state.conn, clip)

    media = cfg.data_dir / clip.rel_path
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"0123456789")

    thumb = cfg.data_dir / clip.thumb_path
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpegbytes")

    return clip


def test_media_serves_the_file_inline(client, stored):
    response = client.get(f"/m/{stored.id}")

    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert response.headers["content-type"] == "video/mp4"
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_media_supports_range_requests(client, stored):
    """Without 206 the player cannot seek — the whole reason /m exists."""
    response = client.get(f"/m/{stored.id}", headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"


def test_media_advertises_range_support(client, stored):
    response = client.get(f"/m/{stored.id}")
    assert response.headers.get("accept-ranges") == "bytes"


def test_thumb_serves_the_thumbnail(client, stored):
    response = client.get(f"/t/{stored.id}")

    assert response.status_code == 200
    assert response.content == b"jpegbytes"
    assert response.headers["content-type"] == "image/jpeg"


def test_download_sets_an_attachment_disposition(client, stored):
    response = client.get(f"/d/{stored.id}")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "halo-aB3xY9z.mp4" in disposition


def test_unknown_id_is_404_on_every_binary_route(client):
    for path in ("/m/nope123", "/t/nope123", "/d/nope123"):
        assert client.get(path).status_code == 404


def test_missing_file_on_disk_is_404_not_500(client, make_clip):
    """A row whose bytes the retention sweep already removed, or a half-restored
    backup. The index is not proof the file is there."""
    db.insert_clip(client.app.state.conn, make_clip("ghost12", created_at=100))
    assert client.get("/m/ghost12").status_code == 404


def test_thumbless_clip_is_404_on_the_thumb_route(client, cfg, make_clip):
    """thumb_path is NULL when ffmpeg failed at ingest. That is a normal row."""
    clip = make_clip("nothumb", created_at=100, thumb=False)
    db.insert_clip(client.app.state.conn, clip)
    media = cfg.data_dir / clip.rel_path
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")

    assert client.get("/t/nothumb").status_code == 404
    assert client.get(f"/m/{clip.id}").status_code == 200


def test_screenshot_is_served_with_its_own_media_type(client, cfg, make_clip):
    clip = make_clip("shot123", created_at=100, kind="screenshot",
                     filename="shot123.png",
                     rel_path="shots/halo/2026/09/shot123.png")
    db.insert_clip(client.app.state.conn, clip)
    path = cfg.data_dir / clip.rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")

    response = client.get("/m/shot123")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd server && python -m pytest tests/test_files.py -v`
Expected: FAIL — every route returns 404 because no router is mounted yet.

- [ ] **Step 3: Promote the path resolver**

In `server/clipd/storage.py`, rename `_resolve_inside` to `resolve_capture` and rewrite its docstring, since Step 3a makes it reachable from the request path:

```python
def resolve_capture(data_dir: Path, rel_path: str) -> Path:
    """Resolve rel_path under data_dir, refusing anything that escapes it.

    Every route that serves bytes goes through this. `Path("/data") / "/etc/x"`
    is `/etc/x`, so an absolute or traversing component silently escapes the
    store without the containment check.
    """
    root = data_dir.resolve()
    target = (data_dir / rel_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"path escapes the data directory: {rel_path!r}")
    return target
```

Update both callers in the same file — `move_capture` (lines 74-75) and `remove_capture` (line 82) — to call `resolve_capture`.

- [ ] **Step 4: Implement the router**

```python
# server/clipd/files.py
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
    return FileResponse(path, media_type=_media_type(clip.rel_path))


@router.get("/t/{clip_id}")
async def thumbnail(request: Request, clip_id: str) -> FileResponse:
    clip = _lookup(request, clip_id)
    path = _existing_path(request.app.state.cfg, clip.thumb_path)
    return FileResponse(path, media_type="image/jpeg")


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
```

- [ ] **Step 5: Mount the router**

In `server/clipd/app.py`, add the import beside the existing ones (line 17):

```python
from . import db, files, media, storage
```

and register the router immediately before `return app` (line 262):

```python
    app.include_router(files.router)

    return app
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd server && python -m pytest tests/test_files.py tests/test_storage.py -v`
Expected: PASS — `test_storage.py` covers `move_capture`/`remove_capture` and must still pass after the rename.

- [ ] **Step 7: Commit and push**

```bash
git add server/clipd/files.py server/clipd/storage.py server/clipd/app.py server/tests/test_files.py
git commit -m "feat: serve media, thumbnails and downloads by clip id"
git push origin feat/clipd-web-ui
```

---

### Task 4: Template layer and the landing page

The first page anyone sees. A Recent strip of the last 8 captures across all games so "did that upload land?" costs no clicks (§8), then a tile per game.

This task carries the scaffolding the next two reuse: the Jinja environment, the base template, the stylesheet, and the static mount.

**Files:**
- Create: `server/clipd/web.py`, `server/clipd/templates/base.html`, `server/clipd/templates/index.html`, `server/clipd/static/app.css`
- Modify: `server/clipd/app.py` (mount static, include the web router), `server/pyproject.toml` (jinja2 dependency)
- Test: `server/tests/test_web_index.py`

**Interfaces:**
- Consumes: `db.list_games`, `db.recent_clips`, `presenters.*`
- Produces:
  - `web.router: APIRouter` serving `GET /`
  - `web.templates: Jinja2Templates` — the shared environment, with the presenters registered as globals
  - `web.page(request, name, **context) -> Response` — the one place `now` enters a template context. It deliberately does NOT thread `cfg`: no 3a template needs it, and `cfg.base_url` matters only for the absolute OpenGraph URLs on 3b's `/c/<slug>`.

- [ ] **Step 1: Add the dependency**

In `server/pyproject.toml`, add to `dependencies`:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "python-multipart>=0.0.12",
    "httpx>=0.27",
    "jinja2>=3.1",
]
```

Then: `cd server && pip install -e ".[dev]"`

- [ ] **Step 2: Write the failing test**

```python
# server/tests/test_web_index.py
"""GET / — the game-first landing page."""
import pytest
from clipd import db



def test_empty_store_renders_an_empty_state(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Nothing captured yet" in response.text


def test_landing_lists_a_tile_per_game(client, make_clip):
    conn = client.app.state.conn
    db.insert_clip(conn, make_clip("a", created_at=100))
    db.insert_clip(conn, make_clip("b", created_at=200))
    db.insert_clip(conn, make_clip("c", created_at=300, game="Doom",
                                   game_slug="doom"))

    body = client.get("/").text

    assert "/g/halo" in body
    assert "/g/doom" in body
    assert "Halo" in body and "Doom" in body


def test_tile_shows_the_capture_count(client, make_clip):
    conn = client.app.state.conn
    for i in range(3):
        db.insert_clip(conn, make_clip(f"c{i}", created_at=i))

    assert "3 captures" in client.get("/").text


def test_tile_shows_a_singular_count_for_one_capture(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("only", created_at=1))
    body = client.get("/").text

    assert "1 capture" in body
    assert "1 captures" not in body


def test_recent_strip_links_each_capture_to_its_detail_page(client, make_clip):
    conn = client.app.state.conn
    db.insert_clip(conn, make_clip("recent1", created_at=500))

    assert "/v/recent1" in client.get("/").text


def test_recent_strip_is_capped(client, make_clip):
    """The strip is a glance, not a listing — /g/<slug> is the listing."""
    conn = client.app.state.conn
    for i in range(20):
        db.insert_clip(conn, make_clip(f"c{i:02d}", created_at=i))

    body = client.get("/").text
    linked = sum(1 for i in range(20) if f"/v/c{i:02d}" in body)
    assert linked == 8


def test_a_game_with_no_thumbnail_still_renders(client, make_clip):
    """thumb_path is NULL whenever ffmpeg failed at ingest."""
    db.insert_clip(client.app.state.conn,
                   make_clip("nothumb", created_at=1, thumb=False))

    assert client.get("/").status_code == 200


def test_html_escapes_a_hostile_game_name(client, make_clip):
    """game comes from a window title on the client (spec §5) — untrusted."""
    db.insert_clip(client.app.state.conn, make_clip(
        "xss1234", created_at=1, game="<script>alert(1)</script>",
        game_slug="script-alert-1-script"))

    body = client.get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd server && python -m pytest tests/test_web_index.py -v`
Expected: FAIL — 404 on `/`, since no route is mounted.

- [ ] **Step 4: Write the web module**

```python
# server/clipd/web.py
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
```

- [ ] **Step 5: Write the base template**

```html
{# server/clipd/templates/base.html #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{% block title %}clipd{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <header class="topbar">
    <a class="brand" href="/">clipd</a>
    {% block subnav %}{% endblock %}
  </header>
  <main class="page">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 6: Write the landing template**

```html
{# server/clipd/templates/index.html #}
{% extends "base.html" %}

{% block content %}
  {% if not games %}
    <p class="empty">Nothing captured yet. Press the replay hotkey and it lands here.</p>
  {% else %}
    {% if recent %}
      <section class="strip">
        <h2 class="section-title">Recent</h2>
        <div class="strip-rail">
          {% for clip in recent %}
            <a class="strip-item" href="/v/{{ clip.id }}">
              <div class="thumb">
                {% if clip.thumb_path %}
                  <img src="/t/{{ clip.id }}" alt="" loading="lazy">
                {% endif %}
                {% if clip.duration_s %}
                  <span class="badge">{{ human_duration(clip.duration_s) }}</span>
                {% endif %}
              </div>
              <span class="strip-name">{{ display_title(clip) }}</span>
              <span class="strip-when">{{ relative_time(clip.created_at, now) }}</span>
            </a>
          {% endfor %}
        </div>
      </section>
    {% endif %}

    <section>
      <h2 class="section-title">Games</h2>
      <div class="tiles">
        {% for game in games %}
          <a class="tile" href="/g/{{ game.game_slug }}">
            <div class="thumb">
              {% if game.cover_id %}
                <img src="/t/{{ game.cover_id }}" alt="" loading="lazy">
              {% endif %}
            </div>
            <span class="tile-name">{{ game.game or "Unknown" }}</span>
            <span class="tile-meta">
              {{ game.clips }} capture{{ "" if game.clips == 1 else "s" }}
              · {{ human_bytes(game.bytes) }}
            </span>
          </a>
        {% endfor %}
      </div>
    </section>
  {% endif %}
{% endblock %}
```

- [ ] **Step 7: Write the stylesheet**

```css
/* server/clipd/static/app.css
   Dark and media-forward (spec §8a #2): the thumbnail is the content, so the
   chrome stays quiet and the grid does the talking. One file, no framework —
   the whole UI is three pages. */

:root {
  --bg: #0b0d10;
  --surface: #14181d;
  --surface-hi: #1c2128;
  --line: #262c34;
  --text: #e6e9ee;
  --muted: #8b95a3;
  --accent: #5aa9ff;
  --radius: 10px;
  --gap: 18px;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}

a { color: inherit; text-decoration: none; }

.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 24px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  position: sticky;
  top: 0;
  z-index: 10;
}

.brand { font-weight: 650; letter-spacing: -0.01em; }
.crumb { color: var(--muted); }
.crumb strong { color: var(--text); font-weight: 600; }

.page { padding: 24px; max-width: 1400px; margin: 0 auto; }

.section-title {
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
  margin: 0 0 12px;
  font-weight: 600;
}

.empty { color: var(--muted); padding: 64px 0; text-align: center; }

/* A thumbnail is 16:9 whether or not an image loaded, so a failed thumbnail
   leaves a hole of the right shape instead of collapsing the grid row. */
.thumb {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--surface-hi);
  border-radius: var(--radius);
  overflow: hidden;
}
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }

.badge {
  position: absolute;
  right: 6px;
  bottom: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.75);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.strip { margin-bottom: 32px; }
.strip-rail {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 210px;
  gap: var(--gap);
  overflow-x: auto;
  padding-bottom: 6px;
  scrollbar-width: thin;
}
.strip-name { display: block; margin-top: 8px; font-size: 14px; }
.strip-when { display: block; color: var(--muted); font-size: 13px; }

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--gap);
}
.tile-name { display: block; margin-top: 8px; font-weight: 550; }
.tile-meta { display: block; color: var(--muted); font-size: 13px; }

.tile:hover .thumb,
.strip-item:hover .thumb,
.card:hover .thumb { outline: 2px solid var(--accent); }

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--gap);
}
.card-name { display: block; margin-top: 8px; font-size: 14px; }
.card-meta { display: block; color: var(--muted); font-size: 13px; }

.chips { display: flex; gap: 8px; margin-bottom: 18px; }
.chip {
  padding: 5px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  font-size: 13px;
}
.chip[aria-current="true"] {
  background: var(--surface-hi);
  border-color: var(--accent);
  color: var(--text);
}

.player { width: 100%; max-height: 76vh; background: #000; border-radius: var(--radius); }
.player img { width: 100%; height: auto; display: block; }

.detail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin: 18px 0 6px;
  flex-wrap: wrap;
}
.detail-title { margin: 0; font-size: 21px; font-weight: 600; }
.detail-meta { color: var(--muted); font-size: 14px; }
.detail-meta dt { display: none; }
.detail-meta dd { display: inline; margin: 0; }
.detail-meta dd + dd::before { content: " · "; }

.actions { display: flex; gap: 10px; }
.button {
  padding: 7px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-hi);
  font-size: 14px;
}
.button:hover { border-color: var(--accent); }

.pager { display: flex; justify-content: center; padding: 28px 0 8px; }
```

- [ ] **Step 8: Mount the templates and static files**

Task 3 already registered `files.router`. Do **not** register it again — a
second `include_router` for the same router duplicates every `/m`, `/t`, `/d`
route. This step adds only the static mount and the web router.

In `server/clipd/app.py`, extend the import line Task 3 edited:

```python
from fastapi.staticfiles import StaticFiles

from . import db, files, media, storage, web
```

and extend the block Task 3 added before `return app`, so it reads:

```python
    app.mount(
        "/static",
        StaticFiles(directory=web.TEMPLATE_DIR.parent / "static"),
        name="static",
    )
    app.include_router(files.router)   # added by Task 3 — leave it as it is
    app.include_router(web.router)

    return app
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `cd server && python -m pytest tests/test_web_index.py tests/test_queries.py -v`
Expected: PASS

- [ ] **Step 10: Commit and push**

```bash
git add server/clipd/web.py server/clipd/templates server/clipd/static \
        server/clipd/app.py server/clipd/db.py server/pyproject.toml \
        server/tests/test_web_index.py server/tests/test_queries.py
git commit -m "feat: add the game-first landing page"
git push origin feat/clipd-web-ui
```

---
### Task 5: The game page

One game's captures, newest first, with clip/screenshot filter chips and keyset pagination (§8, §8a).

**Files:**
- Create: `server/clipd/templates/game.html`
- Modify: `server/clipd/web.py` (add the route and the cursor helpers)
- Test: `server/tests/test_web_game.py`

**Interfaces:**
- Consumes: `db.list_by_game`, `db.count_by_game`, `web.page`, `web.PAGE_SIZE`
- Produces:
  - `web.PAGE_SIZE: int = 48`
  - `web.encode_cursor(clip: Clip) -> str`
  - `web.decode_cursor(raw: str | None) -> tuple[int, str] | None`
  - `GET /g/{game_slug}` accepting `?kind=clip|screenshot` and `?before=<cursor>`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_web_game.py
"""GET /g/<game-slug> — one game's captures."""
import re

import pytest
from clipd import db, web



def seed(client, make_clip, count, **kw):
    conn = client.app.state.conn
    for i in range(count):
        db.insert_clip(conn, make_clip(f"c{i:03d}", created_at=1000 + i, **kw))


def test_unknown_game_is_404(client):
    assert client.get("/g/never-played").status_code == 404


def test_game_page_lists_its_captures(client, make_clip):
    seed(client, make_clip, 3)

    body = client.get("/g/halo").text

    assert "/v/c000" in body and "/v/c002" in body
    assert "Halo" in body


def test_game_page_shows_the_total_count(client, make_clip):
    seed(client, make_clip, 5)
    assert "5 captures" in client.get("/g/halo").text


def test_kind_filter_narrows_the_listing(client, make_clip):
    conn = client.app.state.conn
    db.insert_clip(conn, make_clip("aclip", created_at=100))
    db.insert_clip(conn, make_clip("ashot", created_at=200, kind="screenshot"))

    clips_only = client.get("/g/halo?kind=clip").text
    assert "/v/aclip" in clips_only and "/v/ashot" not in clips_only

    shots_only = client.get("/g/halo?kind=screenshot").text
    assert "/v/ashot" in shots_only and "/v/aclip" not in shots_only


def test_an_unrecognised_kind_is_ignored_rather_than_erroring(client, make_clip):
    """A hand-edited URL should show everything, not a stack trace."""
    seed(client, make_clip, 2)
    response = client.get("/g/halo?kind=nonsense")

    assert response.status_code == 200
    assert "/v/c000" in response.text


def test_a_full_first_page_offers_a_next_link(client, make_clip):
    seed(client, make_clip, web.PAGE_SIZE + 1)

    body = client.get("/g/halo").text

    assert "before=" in body


def test_a_short_page_offers_no_next_link(client, make_clip):
    seed(client, make_clip, 3)
    assert "before=" not in client.get("/g/halo").text


def test_the_next_link_returns_the_following_page(client, make_clip):
    seed(client, make_clip, web.PAGE_SIZE + 3)

    first = client.get("/g/halo")
    # The oldest row on page one is the cursor; page two starts below it.
    oldest = db.list_by_game(client.app.state.conn, "halo",
                             limit=web.PAGE_SIZE)[-1]
    second = client.get(f"/g/halo?before={web.encode_cursor(oldest)}")

    assert second.status_code == 200
    assert f"/v/{oldest.id}" not in second.text
    assert "/v/c000" in second.text


def test_the_kind_filter_survives_pagination(client, make_clip):
    """The Older link must carry the filter, or page two silently widens it."""
    seed(client, make_clip, web.PAGE_SIZE + 1)
    body = client.get("/g/halo?kind=clip").text

    assert re.search(r'href="/g/halo\?before=[^"]*&amp;kind=clip"', body)


def test_a_malformed_cursor_serves_the_first_page(client, make_clip):
    """A truncated or hand-edited URL should not 500."""
    seed(client, make_clip, 3)

    for bad in ("", "garbage", "123", "notanumber_abc", "1_2_3"):
        response = client.get(f"/g/halo?before={bad}")
        assert response.status_code == 200, bad
        assert "/v/c002" in response.text


def test_cursor_roundtrips(make_clip):
    clip = make_clip("aB3xY9z", created_at=1757260800)
    assert web.encode_cursor(clip) == "1757260800_aB3xY9z"
    assert web.decode_cursor("1757260800_aB3xY9z") == (1757260800, "aB3xY9z")


def test_decode_cursor_rejects_junk():
    for bad in (None, "", "x", "a_b", "1_", "_1"):
        assert web.decode_cursor(bad) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd server && python -m pytest tests/test_web_game.py -v`
Expected: FAIL — `AttributeError: module 'clipd.web' has no attribute 'PAGE_SIZE'`

- [ ] **Step 3: Add the route and cursor helpers**

Extend the existing FastAPI import at the top of `server/clipd/web.py` — do
not append a second import line further down the module:

```python
from fastapi import APIRouter, HTTPException, Request
```

Then append to the same file:

```python
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
    if not created_at.isdigit() or not clip_id:
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
```

- [ ] **Step 4: Write the game template**

```html
{# server/clipd/templates/game.html #}
{% extends "base.html" %}

{% block title %}{{ game_name }} · clipd{% endblock %}

{% block subnav %}
  <span class="crumb">/ <strong>{{ game_name }}</strong></span>
{% endblock %}

{% block content %}
  <h2 class="section-title">{{ pluralize(total, "capture") }}</h2>

  <nav class="chips">
    <a class="chip" href="/g/{{ game_slug }}"
       aria-current="{{ 'true' if not kind else 'false' }}">All</a>
    <a class="chip" href="/g/{{ game_slug }}?kind=clip"
       aria-current="{{ 'true' if kind == 'clip' else 'false' }}">Clips</a>
    <a class="chip" href="/g/{{ game_slug }}?kind=screenshot"
       aria-current="{{ 'true' if kind == 'screenshot' else 'false' }}">Screenshots</a>
  </nav>

  {% if not clips %}
    <p class="empty">Nothing here with that filter.</p>
  {% else %}
    <div class="cards">
      {% for clip in clips %}
        <a class="card" href="/v/{{ clip.id }}">
          <div class="thumb">
            {% if clip.thumb_path %}
              <img src="/t/{{ clip.id }}" alt="" loading="lazy">
            {% endif %}
            {% if clip.duration_s %}
              <span class="badge">{{ human_duration(clip.duration_s) }}</span>
            {% endif %}
          </div>
          <span class="card-name">{{ display_title(clip) }}</span>
          <span class="card-meta">
            {{ relative_time(clip.created_at, now) }} · {{ human_bytes(clip.bytes) }}
          </span>
        </a>
      {% endfor %}
    </div>

    {% if next_cursor %}
      <div class="pager">
        <a class="button"
           href="/g/{{ game_slug }}?before={{ next_cursor }}{% if kind %}&kind={{ kind }}{% endif %}">
          Older
        </a>
      </div>
    {% endif %}
  {% endif %}
{% endblock %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server && python -m pytest tests/test_web_game.py -v`
Expected: PASS

- [ ] **Step 6: Commit and push**

```bash
git add server/clipd/web.py server/clipd/templates/game.html server/tests/test_web_game.py
git commit -m "feat: add the per-game listing with keyset pagination"
git push origin feat/clipd-web-ui
```

---

### Task 6: The clip detail page

What `/v/<id>` — the URL already on the operator's clipboard since Step 1 (`app.py:88`) — finally resolves to.

**Files:**
- Create: `server/clipd/templates/clip.html`, `server/clipd/static/app.js`
- Modify: `server/clipd/web.py` (add the route), `server/clipd/templates/base.html` (load the script)
- Test: `server/tests/test_web_clip.py`

**Interfaces:**
- Consumes: `db.get_by_id`, `web.page`, `presenters.*`
- Produces: `GET /v/{clip_id}`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_web_clip.py
"""GET /v/<id> — the detail page the clipboard URL points at."""
from clipd import db



def test_unknown_clip_is_404(client):
    assert client.get("/v/nope123").status_code == 404


def test_a_clip_renders_a_video_player_pointed_at_the_media_route(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))

    body = client.get("/v/aB3xY9z").text

    assert "<video" in body
    assert "/m/aB3xY9z" in body


def test_a_screenshot_renders_an_image_not_a_player(client, make_clip):
    clip = make_clip("shot123", created_at=100, kind="screenshot",
                     duration_s=None,
                     rel_path="shots/halo/2026/09/shot123.png")
    db.insert_clip(client.app.state.conn, clip)

    body = client.get("/v/shot123").text

    assert "<video" not in body
    assert "<img" in body and "/m/shot123" in body


def test_detail_page_offers_a_download(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))
    assert "/d/aB3xY9z" in client.get("/v/aB3xY9z").text


def test_detail_page_links_back_to_the_game(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))
    assert "/g/halo" in client.get("/v/aB3xY9z").text


def test_detail_page_shows_resolution_duration_and_size(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))

    body = client.get("/v/aB3xY9z").text

    assert "1920×1080" in body
    assert "1:30" in body
    assert "1000 B" in body


def test_a_clip_with_no_probe_data_still_renders(client, make_clip):
    """ffprobe can fail at ingest; those columns are nullable for that reason."""
    clip = make_clip("sparse1", created_at=100, duration_s=None,
                     width=None, height=None)
    db.insert_clip(client.app.state.conn, clip)

    assert client.get("/v/sparse1").status_code == 200


def test_detail_page_escapes_a_hostile_title(client, make_clip):
    clip = make_clip("xss1234", created_at=100,
                     title="<img src=x onerror=alert(1)>")
    db.insert_clip(client.app.state.conn, clip)

    body = client.get("/v/xss1234").text

    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img" in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd server && python -m pytest tests/test_web_clip.py -v`
Expected: FAIL — 404 on `/v/aB3xY9z`, no route registered.

- [ ] **Step 3: Add the route**

Append to `server/clipd/web.py`:

```python
@router.get("/v/{clip_id}", response_class=HTMLResponse)
async def clip_page(request: Request, clip_id: str) -> HTMLResponse:
    conn: sqlite3.Connection = request.app.state.conn
    clip = db.get_by_id(conn, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="not found")
    return page(request, "clip.html", clip=clip)
```

- [ ] **Step 4: Write the detail template**

```html
{# server/clipd/templates/clip.html #}
{% extends "base.html" %}

{% block title %}{{ display_title(clip) }} · clipd{% endblock %}

{% block subnav %}
  <span class="crumb">
    / <a href="/g/{{ clip.game_slug }}">{{ clip.game or "Unknown" }}</a>
  </span>
{% endblock %}

{% block content %}
  {% if clip.kind == "clip" %}
    <video class="player" src="/m/{{ clip.id }}" controls autoplay
           preload="metadata" playsinline></video>
  {% else %}
    <div class="player"><img src="/m/{{ clip.id }}" alt="{{ display_title(clip) }}"></div>
  {% endif %}

  <div class="detail-head">
    <h1 class="detail-title">{{ display_title(clip) }}</h1>
    <div class="actions">
      <a class="button" href="/d/{{ clip.id }}">Download</a>
    </div>
  </div>

  <dl class="detail-meta">
    <dt>Captured</dt><dd>{{ relative_time(clip.created_at, now) }}</dd>
    {% if clip.duration_s %}
      <dt>Duration</dt><dd>{{ human_duration(clip.duration_s) }}</dd>
    {% endif %}
    {% if clip.width and clip.height %}
      <dt>Resolution</dt><dd>{{ clip.width }}×{{ clip.height }}</dd>
    {% endif %}
    <dt>Size</dt><dd>{{ human_bytes(clip.bytes) }}</dd>
    <dt>Source</dt><dd>{{ clip.source_host }}</dd>
  </dl>
{% endblock %}
```

- [ ] **Step 5: Add the keyboard shortcuts**

```javascript
// server/clipd/static/app.js
// The only client-side code in Step 3a. Everything else is a link.
(function () {
  "use strict";

  var video = document.querySelector("video.player");

  document.addEventListener("keydown", function (event) {
    // Never steal a key from a field, and never from a browser shortcut.
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;

    if (event.key === "Escape") {
      var back = document.querySelector(".crumb a");
      if (back) window.location.href = back.href;
      return;
    }

    if (!video) return;

    if (event.key === " ") {
      event.preventDefault();
      video.paused ? video.play() : video.pause();
    } else if (event.key === "ArrowLeft") {
      video.currentTime -= 5;
    } else if (event.key === "ArrowRight") {
      video.currentTime += 5;
    }
  });
})();
```

Add the script to `base.html`, immediately before `</body>`:

```html
  <script src="/static/app.js" defer></script>
</body>
```

- [ ] **Step 6: Run the whole suite**

Run: `cd server && python -m pytest -v`
Expected: PASS — every test, including the Step 1 suites.

- [ ] **Step 7: Commit and push**

```bash
git add server/clipd/web.py server/clipd/templates server/clipd/static \
        server/tests/test_web_clip.py
git commit -m "feat: add the clip detail page and player"
git push origin feat/clipd-web-ui
```

---

### Task 7: Ship the templates in the image, and document the routes

The Dockerfile installs with `pip install .` (`server/Dockerfile:13`). `setuptools.packages.find` collects Python modules only — `templates/` and `static/` are data, so without an explicit declaration they are absent from the installed package.

**This is a startup crash, not a degraded page.** `app.py` constructs `StaticFiles(directory=...)` inside `create_app`, and Starlette's `StaticFiles.__init__` raises `RuntimeError: Directory '...' does not exist` immediately when the directory is missing — verified against the installed Starlette. So the container dies on boot, the healthcheck never passes, and compose restarts it forever. Every test passes locally and the image builds clean, because the source tree is importable in development either way. This task is the only thing standing between the branch and a dead deploy.

**Files:**
- Modify: `server/pyproject.toml` (package-data), `README.md` (route table, Step status)
- Test: `server/tests/test_packaging.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing importable — this task's deliverable is a correct wheel

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_packaging.py
"""Templates and static files must survive `pip install .`.

They are data, not modules, so setuptools does not collect them by default.
The failure mode is invisible in development — the source tree is on the path
either way — and only appears as a 500 inside the container.
"""
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
PACKAGE = Path(__file__).resolve().parents[1] / "clipd"


def test_templates_and_static_are_declared_as_package_data():
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    package_data = raw["tool"]["setuptools"]["package-data"]["clipd"]

    assert "templates/*.html" in package_data
    assert "static/*" in package_data


def test_every_template_on_disk_is_covered_by_the_declaration():
    """A .html added to a subdirectory would not match templates/*.html."""
    stray = [p for p in (PACKAGE / "templates").rglob("*.html")
             if p.parent != PACKAGE / "templates"]
    assert stray == [], f"nested templates are not packaged: {stray}"


def test_jinja2_is_a_runtime_dependency():
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert any(d.startswith("jinja2") for d in raw["project"]["dependencies"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd server && python -m pytest tests/test_packaging.py -v`
Expected: FAIL — `KeyError: 'package-data'`

- [ ] **Step 3: Declare the package data**

Append to `server/pyproject.toml`:

```toml
# templates/ and static/ are data, not modules, so packages.find does not
# collect them. The Dockerfile installs with `pip install .`, so without this
# the image ships a clipd that 500s on its first page render — and nothing in
# local development would catch it, because the source tree is importable.
[tool.setuptools.package-data]
clipd = ["templates/*.html", "static/*"]
```

- [ ] **Step 4: Verify the built wheel actually contains them**

Run:
```bash
cd server && python -m pip wheel --no-deps -w /tmp/clipd-wheel . \
  && python -c "
import zipfile, glob
names = zipfile.ZipFile(glob.glob('/tmp/clipd-wheel/clipd-*.whl')[0]).namelist()
missing = [n for n in ('clipd/templates/base.html','clipd/templates/index.html',
                       'clipd/templates/game.html','clipd/templates/clip.html',
                       'clipd/static/app.css','clipd/static/app.js')
           if n not in names]
print('MISSING:', missing or 'nothing')
assert not missing
"
```
Expected: `MISSING: nothing`

- [ ] **Step 5: Update the README**

In `README.md`, replace the Step 1 endpoint table with one covering both steps, and correct the paragraph beneath it that says the gallery is still Step 3:

```markdown
## Endpoints
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/ingest` | Bearer | multipart `file` + JSON `meta`. Returns `{id, url}`. Idempotent on `capture_uuid`. |
| GET | `/healthz` | none | `{status, clips, bytes, budget_bytes, used_pct}` for Uptime Kuma |
| GET | `/` | tailnet | Game tiles + a Recent strip |
| GET | `/g/<game-slug>` | tailnet | One game's captures; `?kind=clip\|screenshot`, `?before=<cursor>` |
| GET | `/v/<id>` | tailnet | Detail page and player — the URL the watcher puts on the clipboard |
| GET | `/m/<id>` | tailnet | The media itself, inline and Range-capable |
| GET | `/t/<id>` | tailnet | Thumbnail |
| GET | `/d/<id>` | tailnet | Download the original |

The tailnet routes carry no application-level auth: Tailscale is the boundary.
See the design spec §8a.

Editing, trimming, deleting, and the dormant share routes are Step 3b.
```

Also update the Layout section's Step markers to note that `server/` now carries Steps 1 and 3a.

- [ ] **Step 6: Run the whole suite one last time**

Run: `cd server && python -m pytest -v && cd ../client && python -m pytest -v`
Expected: PASS on both — the client suite must be unaffected.

- [ ] **Step 7: Commit and push**

```bash
git add server/pyproject.toml server/tests/test_packaging.py README.md
git commit -m "fix: ship templates and static assets in the installed package"
git push origin feat/clipd-web-ui
```

---

## Definition of done

3a is complete when, from a clean checkout:

1. `cd server && python -m pytest` passes, with no network and no `ffmpeg` invocation.
2. `cd client && python -m pytest` still passes.
3. The built wheel contains all four templates and both static files (Task 7, Step 4).
4. `uvicorn clipd.asgi:app` serves `/` with tiles, `/g/<slug>` with a filterable and paginating listing, and `/v/<id>` with a player that **seeks** — the last is the point of `/m/<id>` and Range.
5. No route added in 3a writes to the database or the filesystem.

## Out of scope — do not build these here

`PATCH /api/clips/<id>`, `DELETE /api/clips/<id>`, `POST /api/clips/bulk-delete`, `POST /api/clips/<id>/trim`, `POST /api/clips/<id>/share`, `GET /c/<slug>`, OpenGraph tags, bulk selection UI, the PWA shell. All are Step 3b or deferred (spec §8a, §10).
