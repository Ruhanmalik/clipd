# clipd Server Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy the clipd ingest server so a capture POSTed from any tailnet machine is stored, indexed, thumbnailed, announced via ntfy, and returned as a short URL — verifiable by `curl` before any capture client exists.

**Architecture:** A single FastAPI service behind one SQLite index, deployed by Docker Compose to `/home/batman/clipd/` on midget. Uploads stream to disk under `clips/<game-slug>/YYYY/MM/<id>.mp4`; the only video work the server ever does is extracting one thumbnail frame with ffmpeg. A background timer prunes oldest-first against a size budget. Pure-function modules (`slug`, `ids`), an I/O layer (`db`, `storage`, `media`, `notify`), and a thin route layer (`app`) keep each file to one responsibility.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, stdlib `sqlite3`, httpx, ffmpeg/ffprobe, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-07-clipd-design.md`

## Global Constraints

Every task's requirements implicitly include this section. Values copied verbatim from the spec and from `plan.md`.

- Python **3.12**; base image `python:3.12-slim` plus `ffmpeg`.
- clipd binds port **8000** — confirmed free on midget (`plan.md` §3).
- Deploy target is **`/home/batman/clipd/`**, one directory per service (`plan.md` §4).
- Compose uses **bind mounts** (`./data:/data`), never named volumes (`plan.md` §4).
- `restart: unless-stopped` and `TZ: "America/Chicago"` on every service (`plan.md` §4).
- Secrets live in **`.env`**, never in the compose file and never committed (`plan.md` §4).
- **Do not mount the Docker socket.** clipd does not need it (`plan.md` §4).
- **The server never transcodes.** Its only ffmpeg work is one thumbnail frame per capture, and later `-c copy` trims (`plan.md` §5).
- **Never touch `/dev/dri/renderD128`.** Jellyfin owns QuickSync (`plan.md` §10). clipd needs no GPU at all.
- Compose files carry comments explaining non-obvious, load-bearing decisions (`plan.md` §4).
- All public URLs address clips by `id`, never by path, so re-tagging never breaks a link.
- Retention: **size cap only, no age limit.** Default `MAX_STORE_BYTES=50GB`.
- Retention **never** deletes a row with a non-NULL `public_slug` or `pinned = 1`.

## Prerequisite

SSH key auth from the MacBook to `batman@100.72.75.62` must work before Task 10. Currently refused (`Permission denied (publickey)`). Fix with `ssh-copy-id batman@100.72.75.62`. Tasks 1–9 need no server access.

---

### Task 1: Project scaffold and configuration

**Files:**
- Create: `server/pyproject.toml`
- Create: `server/clipd/__init__.py`
- Create: `server/clipd/config.py`
- Test: `server/tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `Config` frozen dataclass with fields `data_dir: Path`, `ingest_token: str`, `ntfy_topic: str | None`, `ntfy_server: str`, `max_store_bytes: int`, `base_url: str`, `sweep_interval_s: int`, `share_enabled: bool`; classmethod `Config.from_env(env: Mapping[str, str]) -> Config`; module function `parse_size(text: str) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_config.py
import pytest
from pathlib import Path
from clipd.config import Config, parse_size


def test_parse_size_accepts_plain_bytes():
    assert parse_size("1024") == 1024


def test_parse_size_accepts_binary_suffixes():
    assert parse_size("50GB") == 50 * 1024**3
    assert parse_size("512mb") == 512 * 1024**2
    assert parse_size("2 TB") == 2 * 1024**4


def test_parse_size_rejects_garbage():
    with pytest.raises(ValueError):
        parse_size("banana")


def test_from_env_reads_all_fields():
    cfg = Config.from_env({
        "DATA_DIR": "/data",
        "INGEST_TOKEN": "secret",
        "NTFY_TOPIC": "clipd-abc",
        "MAX_STORE_BYTES": "50GB",
        "BASE_URL": "http://midget.tail0056d7.ts.net:8000",
    })
    assert cfg.data_dir == Path("/data")
    assert cfg.ingest_token == "secret"
    assert cfg.ntfy_topic == "clipd-abc"
    assert cfg.max_store_bytes == 50 * 1024**3
    assert cfg.base_url == "http://midget.tail0056d7.ts.net:8000"


def test_from_env_applies_defaults():
    cfg = Config.from_env({"INGEST_TOKEN": "secret"})
    assert cfg.data_dir == Path("/data")
    assert cfg.ntfy_topic is None
    assert cfg.ntfy_server == "https://ntfy.sh"
    assert cfg.max_store_bytes == 50 * 1024**3
    assert cfg.sweep_interval_s == 900
    assert cfg.share_enabled is False


def test_base_url_strips_trailing_slash():
    cfg = Config.from_env({"INGEST_TOKEN": "s", "BASE_URL": "http://host:8000/"})
    assert cfg.base_url == "http://host:8000"


def test_missing_ingest_token_is_fatal():
    with pytest.raises(ValueError, match="INGEST_TOKEN"):
        Config.from_env({})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd'`

- [ ] **Step 3: Write the scaffold and implementation**

```toml
# server/pyproject.toml
[project]
name = "clipd"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "python-multipart>=0.0.12",
    "httpx>=0.27",
    "jinja2>=3.1",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["clipd*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

```python
# server/clipd/__init__.py
"""clipd — self-hosted clip and screenshot capture."""
```

```python
# server/clipd/config.py
"""Environment-backed configuration. Loaded once at startup."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?B?)\s*$", re.IGNORECASE)

# Binary multipliers: "50GB" means 50 GiB. The disk budget on midget is what
# matters here, and `df` reports binary units, so matching it avoids a 7% surprise.
_MULTIPLIERS = {
    "": 1, "B": 1,
    "K": 1024, "KB": 1024,
    "M": 1024**2, "MB": 1024**2,
    "G": 1024**3, "GB": 1024**3,
    "T": 1024**4, "TB": 1024**4,
}


def parse_size(text: str) -> int:
    """Parse '50GB', '512mb', or a plain byte count into an int."""
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"cannot parse size: {text!r}")
    number, suffix = match.groups()
    return int(float(number) * _MULTIPLIERS[suffix.upper()])


@dataclass(frozen=True)
class Config:
    data_dir: Path
    ingest_token: str
    ntfy_topic: str | None
    ntfy_server: str
    max_store_bytes: int
    base_url: str
    sweep_interval_s: int
    share_enabled: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        token = env.get("INGEST_TOKEN", "").strip()
        if not token:
            raise ValueError("INGEST_TOKEN must be set and non-empty")
        return cls(
            data_dir=Path(env.get("DATA_DIR", "/data")),
            ingest_token=token,
            ntfy_topic=env.get("NTFY_TOPIC") or None,
            ntfy_server=env.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            max_store_bytes=parse_size(env.get("MAX_STORE_BYTES", "50GB")),
            base_url=env.get("BASE_URL", "http://localhost:8000").rstrip("/"),
            sweep_interval_s=int(env.get("SWEEP_INTERVAL_S", "900")),
            share_enabled=env.get("SHARE_ENABLED", "").lower() in {"1", "true", "yes"},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && pip install -e ".[dev]" && python -m pytest tests/test_config.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add server/pyproject.toml server/clipd/__init__.py server/clipd/config.py server/tests/test_config.py
git commit -m "feat: add clipd package scaffold and config loading"
```

---

### Task 2: Identifiers and game slugging

**Files:**
- Create: `server/clipd/ids.py`
- Create: `server/clipd/slug.py`
- Test: `server/tests/test_ids.py`
- Test: `server/tests/test_slug.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ids.new_id() -> str` (7-char base62), `ids.new_public_slug() -> str` (22-char base62), `slug.slugify_game(name: str | None) -> str`.

These are pure functions with no I/O, which is why they come before the storage layer — every later task depends on their exact output shape.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_ids.py
from clipd.ids import ALPHABET, new_id, new_public_slug


def test_new_id_is_seven_base62_chars():
    value = new_id()
    assert len(value) == 7
    assert all(c in ALPHABET for c in value)


def test_new_id_is_not_constant():
    assert len({new_id() for _ in range(200)}) > 190


def test_public_slug_has_at_least_16_chars_of_entropy():
    # Spec §7 of plan.md requires >= 16 chars of entropy for unguessable share links.
    value = new_public_slug()
    assert len(value) >= 16
    assert all(c in ALPHABET for c in value)
```

```python
# server/tests/test_slug.py
import pytest
from clipd.slug import slugify_game


@pytest.mark.parametrize("raw,expected", [
    ("Counter-Strike 2", "counter-strike-2"),
    ("VALORANT  ", "valorant"),
    ("EA SPORTS FC 25", "ea-sports-fc-25"),
    ("Rock/Paper", "rock-paper"),
    ("Deep   Rock  Galactic", "deep-rock-galactic"),
    ("Game....", "game"),
    ("  ---Halo---  ", "halo"),
])
def test_slugify_normalizes_names(raw, expected):
    assert slugify_game(raw) == expected


@pytest.mark.parametrize("raw", ['A<B', 'A>B', 'A:B', 'A"B', 'A/B', "A\\B", "A|B", "A?B", "A*B"])
def test_slugify_strips_windows_illegal_characters(raw):
    result = slugify_game(raw)
    assert not any(c in result for c in '<>:"/\\|?*')


@pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9"])
def test_slugify_escapes_windows_reserved_names(reserved):
    # A folder literally named CON cannot be created on Windows, and the watcher
    # may stage paths locally before upload.
    assert slugify_game(reserved) == f"{reserved.lower()}-game"


@pytest.mark.parametrize("raw", [None, "", "   ", "???", "\x00\x01"])
def test_slugify_falls_back_to_unknown(raw):
    assert slugify_game(raw) == "unknown"


def test_slugify_is_idempotent():
    once = slugify_game("Counter-Strike 2")
    assert slugify_game(once) == once
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/test_ids.py tests/test_slug.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.ids'`

- [ ] **Step 3: Write the implementations**

```python
# server/clipd/ids.py
"""URL-safe random identifiers."""
from __future__ import annotations

import secrets

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

ID_LENGTH = 7          # ~62^7 = 3.5e12, ample for a personal store
PUBLIC_SLUG_LENGTH = 22  # ~131 bits; plan.md §7 requires >= 16 chars of entropy


def _random_string(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_id() -> str:
    return _random_string(ID_LENGTH)


def new_public_slug() -> str:
    return _random_string(PUBLIC_SLUG_LENGTH)
```

```python
# server/clipd/slug.py
"""Turn a game's display name into a path-safe directory component.

Must be valid on Linux (the server) and Windows (the watcher stages files
locally before upload), so the Windows rules are the binding ones.
"""
from __future__ import annotations

import re
import unicodedata

_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NON_SLUG = re.compile(r"[^a-z0-9]+")

FALLBACK = "unknown"


def slugify_game(name: str | None) -> str:
    if not name:
        return FALLBACK

    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _ILLEGAL.sub(" ", text)
    text = _NON_SLUG.sub("-", text.lower())
    text = text.strip("-.")

    if not text:
        return FALLBACK
    if text.upper() in _WINDOWS_RESERVED:
        return f"{text}-game"
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && python -m pytest tests/test_ids.py tests/test_slug.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add server/clipd/ids.py server/clipd/slug.py server/tests/test_ids.py server/tests/test_slug.py
git commit -m "feat: add id generation and windows-safe game slugging"
```

---

### Task 3: Database layer

**Files:**
- Create: `server/clipd/db.py`
- Test: `server/tests/test_db.py`

**Interfaces:**
- Consumes: nothing at runtime (rows are supplied by callers)
- Produces: `Clip` dataclass; `connect(path: Path) -> sqlite3.Connection`; `init_schema(conn) -> None`; `insert_clip(conn, clip: Clip) -> None`; `get_by_id(conn, clip_id: str) -> Clip | None`; `get_by_capture_uuid(conn, capture_uuid: str) -> Clip | None`; `store_totals(conn) -> tuple[int, int]` returning `(count, bytes)`; `prunable_oldest_first(conn) -> list[Clip]`; `unprunable_bytes(conn) -> int`; `delete_clip(conn, clip_id: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_db.py
import pytest
from clipd.db import (
    Clip, connect, init_schema, insert_clip, get_by_id, get_by_capture_uuid,
    store_totals, prunable_oldest_first, unprunable_bytes, delete_clip,
)


def make_clip(**overrides) -> Clip:
    base = dict(
        id="aB3xY9z", public_slug=None, capture_uuid="uuid-1", kind="clip",
        title=None, filename="aB3xY9z.mp4",
        rel_path="clips/counter-strike-2/2026/09/aB3xY9z.mp4",
        bytes=1000, duration_s=90.0, width=1920, height=1080,
        game="Counter-Strike 2", game_slug="counter-strike-2", game_exe="cs2.exe",
        pinned=0, source_host="desktop-amtr56i", created_at=1757260800,
        thumb_path="thumbs/aB3xY9z.jpg",
    )
    base.update(overrides)
    return Clip(**base)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


def test_insert_and_get_roundtrip(conn):
    clip = make_clip()
    insert_clip(conn, clip)
    assert get_by_id(conn, "aB3xY9z") == clip


def test_get_by_id_returns_none_when_absent(conn):
    assert get_by_id(conn, "nope123") is None


def test_get_by_capture_uuid_finds_the_row(conn):
    insert_clip(conn, make_clip())
    found = get_by_capture_uuid(conn, "uuid-1")
    assert found is not None and found.id == "aB3xY9z"


def test_capture_uuid_is_unique(conn):
    insert_clip(conn, make_clip())
    with pytest.raises(Exception):
        insert_clip(conn, make_clip(id="different"))


def test_init_schema_is_idempotent(conn, tmp_path):
    init_schema(conn)  # second call must not raise
    insert_clip(conn, make_clip())
    assert store_totals(conn) == (1, 1000)


def test_store_totals_sums_count_and_bytes(conn):
    insert_clip(conn, make_clip(id="a", capture_uuid="u1", bytes=100))
    insert_clip(conn, make_clip(id="b", capture_uuid="u2", bytes=250))
    assert store_totals(conn) == (2, 350)


def test_store_totals_on_empty_store(conn):
    assert store_totals(conn) == (0, 0)


def test_prunable_excludes_pinned_and_shared(conn):
    insert_clip(conn, make_clip(id="old", capture_uuid="u1", created_at=100))
    insert_clip(conn, make_clip(id="pin", capture_uuid="u2", created_at=200, pinned=1))
    insert_clip(conn, make_clip(id="shr", capture_uuid="u3", created_at=300,
                                public_slug="sharedslug1234567890ab"))
    insert_clip(conn, make_clip(id="new", capture_uuid="u4", created_at=400))

    ids = [c.id for c in prunable_oldest_first(conn)]
    assert ids == ["old", "new"]  # oldest first, protected rows omitted


def test_unprunable_bytes_counts_only_protected_rows(conn):
    insert_clip(conn, make_clip(id="a", capture_uuid="u1", bytes=100))
    insert_clip(conn, make_clip(id="b", capture_uuid="u2", bytes=200, pinned=1))
    insert_clip(conn, make_clip(id="c", capture_uuid="u3", bytes=400,
                                public_slug="slugslugslugslugslug12"))
    assert unprunable_bytes(conn) == 600


def test_delete_clip_removes_the_row(conn):
    insert_clip(conn, make_clip())
    delete_clip(conn, "aB3xY9z")
    assert get_by_id(conn, "aB3xY9z") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.db'`

- [ ] **Step 3: Write the implementation**

```python
# server/clipd/db.py
"""SQLite index. Exactly one writer, so no connection pool and no Postgres."""
from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass, fields
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
  id           TEXT PRIMARY KEY,
  public_slug  TEXT UNIQUE,
  capture_uuid TEXT UNIQUE,
  kind         TEXT NOT NULL,
  title        TEXT,
  filename     TEXT NOT NULL,
  rel_path     TEXT NOT NULL,
  bytes        INTEGER NOT NULL,
  duration_s   REAL,
  width        INTEGER,
  height       INTEGER,
  game         TEXT,
  game_slug    TEXT NOT NULL,
  game_exe     TEXT,
  pinned       INTEGER NOT NULL DEFAULT 0,
  source_host  TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  thumb_path   TEXT
);
CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clips_game    ON clips(game_slug);
CREATE INDEX IF NOT EXISTS idx_clips_pinned  ON clips(pinned) WHERE pinned = 1;
"""

# A row is protected from the retention sweep if someone holds a share link for
# it, or it was explicitly pinned. See spec §7.
_PROTECTED = "(public_slug IS NOT NULL OR pinned = 1)"


@dataclass(frozen=True)
class Clip:
    id: str
    public_slug: str | None
    capture_uuid: str
    kind: str
    title: str | None
    filename: str
    rel_path: str
    bytes: int
    duration_s: float | None
    width: int | None
    height: int | None
    game: str | None
    game_slug: str
    game_exe: str | None
    pinned: int
    source_host: str
    created_at: int
    thumb_path: str | None


_COLUMNS = [f.name for f in fields(Clip)]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets the retention sweep read while a request writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _to_clip(row: sqlite3.Row | None) -> Clip | None:
    return Clip(**{k: row[k] for k in _COLUMNS}) if row else None


def insert_clip(conn: sqlite3.Connection, clip: Clip) -> None:
    placeholders = ", ".join("?" * len(_COLUMNS))
    conn.execute(
        f"INSERT INTO clips ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        astuple(clip),
    )
    conn.commit()


def get_by_id(conn: sqlite3.Connection, clip_id: str) -> Clip | None:
    cur = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,))
    return _to_clip(cur.fetchone())


def get_by_capture_uuid(conn: sqlite3.Connection, capture_uuid: str) -> Clip | None:
    cur = conn.execute("SELECT * FROM clips WHERE capture_uuid = ?", (capture_uuid,))
    return _to_clip(cur.fetchone())


def store_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM clips")
    count, total = cur.fetchone()
    return int(count), int(total)


def prunable_oldest_first(conn: sqlite3.Connection) -> list[Clip]:
    cur = conn.execute(
        f"SELECT * FROM clips WHERE NOT {_PROTECTED} ORDER BY created_at ASC"
    )
    return [c for c in (_to_clip(r) for r in cur.fetchall()) if c is not None]


def unprunable_bytes(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        f"SELECT COALESCE(SUM(bytes), 0) FROM clips WHERE {_PROTECTED}"
    )
    return int(cur.fetchone()[0])


def delete_clip(conn: sqlite3.Connection, clip_id: str) -> None:
    conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_db.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add server/clipd/db.py server/tests/test_db.py
git commit -m "feat: add sqlite index with retention-aware queries"
```

---

### Task 4: Storage paths and streaming writes

**Files:**
- Create: `server/clipd/storage.py`
- Test: `server/tests/test_storage.py`

**Interfaces:**
- Consumes: `clipd.slug.slugify_game`
- Produces: `rel_path_for(kind: str, game_slug: str, created_at: int, clip_id: str, ext: str) -> str`; `thumb_rel_path(clip_id: str) -> str`; `async write_stream(dest: Path, chunks: AsyncIterator[bytes]) -> int`; `move_capture(data_dir: Path, old_rel: str, new_rel: str) -> None`; `remove_capture(data_dir: Path, rel_path: str) -> None`.

`write_stream` must never load the whole upload into memory — a 90-second clip can be several hundred megabytes and midget has 14 GiB total.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_storage.py
import pytest
from pathlib import Path
from clipd.storage import (
    rel_path_for, thumb_rel_path, write_stream, move_capture, remove_capture,
)


async def chunks_of(*blobs):
    for blob in blobs:
        yield blob


def test_rel_path_for_clip_is_game_then_date():
    path = rel_path_for("clip", "counter-strike-2", 1757260800, "aB3xY9z", ".mp4")
    assert path == "clips/counter-strike-2/2025/09/aB3xY9z.mp4"


def test_rel_path_for_screenshot_uses_shots_root():
    path = rel_path_for("screenshot", "valorant", 1757260800, "xY7z", ".png")
    assert path == "shots/valorant/2025/09/xY7z.png"


def test_rel_path_rejects_unknown_kind():
    with pytest.raises(ValueError):
        rel_path_for("video", "halo", 1757260800, "a", ".mp4")


def test_thumb_rel_path():
    assert thumb_rel_path("aB3xY9z") == "thumbs/aB3xY9z.jpg"


async def test_write_stream_creates_parents_and_returns_size(tmp_path):
    dest = tmp_path / "clips" / "halo" / "2026" / "09" / "a.mp4"
    written = await write_stream(dest, chunks_of(b"abc", b"defg"))
    assert written == 7
    assert dest.read_bytes() == b"abcdefg"


async def test_write_stream_cleans_up_on_failure(tmp_path):
    async def exploding():
        yield b"partial"
        raise IOError("network died")

    dest = tmp_path / "clips" / "halo" / "a.mp4"
    with pytest.raises(IOError):
        await write_stream(dest, exploding())
    # A half-written file must not survive to be indexed as a real clip.
    assert not dest.exists()


async def test_move_capture_relocates_and_prunes_empty_dirs(tmp_path):
    old = "clips/unknown/2026/09/a.mp4"
    new = "clips/halo/2026/09/a.mp4"
    await write_stream(tmp_path / old, chunks_of(b"data"))

    move_capture(tmp_path, old, new)

    assert (tmp_path / new).read_bytes() == b"data"
    assert not (tmp_path / old).exists()
    assert not (tmp_path / "clips" / "unknown").exists()


async def test_remove_capture_deletes_file_and_empty_dirs(tmp_path):
    rel = "clips/halo/2026/09/a.mp4"
    await write_stream(tmp_path / rel, chunks_of(b"data"))

    remove_capture(tmp_path, rel)

    assert not (tmp_path / rel).exists()
    assert not (tmp_path / "clips" / "halo").exists()


def test_remove_capture_is_safe_when_file_is_already_gone(tmp_path):
    remove_capture(tmp_path, "clips/halo/2026/09/missing.mp4")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_storage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.storage'`

- [ ] **Step 3: Write the implementation**

```python
# server/clipd/storage.py
"""On-disk layout: game first (browsable), date beneath (bounded directories)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

CHUNK = 1024 * 1024  # 1 MiB

_ROOTS = {"clip": "clips", "screenshot": "shots"}


def rel_path_for(kind: str, game_slug: str, created_at: int, clip_id: str, ext: str) -> str:
    try:
        root = _ROOTS[kind]
    except KeyError:
        raise ValueError(f"unknown kind: {kind!r}") from None
    when = datetime.fromtimestamp(created_at, tz=timezone.utc)
    return f"{root}/{game_slug}/{when:%Y}/{when:%m}/{clip_id}{ext}"


def thumb_rel_path(clip_id: str) -> str:
    return f"thumbs/{clip_id}.jpg"


async def write_stream(dest: Path, chunks: AsyncIterator[bytes]) -> int:
    """Stream chunks to dest, returning bytes written.

    Writes to a .part file first: a half-uploaded clip must never be visible at
    its final path, because the watcher retries and the sweep walks these dirs.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    total = 0
    try:
        with partial.open("wb") as handle:
            async for chunk in chunks:
                handle.write(chunk)
                total += len(chunk)
        partial.replace(dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return total


def _prune_empty_parents(data_dir: Path, path: Path) -> None:
    """Walk up removing now-empty directories, stopping at data_dir."""
    parent = path.parent
    while parent != data_dir and parent.is_relative_to(data_dir):
        try:
            parent.rmdir()
        except OSError:
            return  # not empty, or gone — either way we are done
        parent = parent.parent


def move_capture(data_dir: Path, old_rel: str, new_rel: str) -> None:
    old, new = data_dir / old_rel, data_dir / new_rel
    new.parent.mkdir(parents=True, exist_ok=True)
    old.replace(new)
    _prune_empty_parents(data_dir, old)


def remove_capture(data_dir: Path, rel_path: str) -> None:
    target = data_dir / rel_path
    target.unlink(missing_ok=True)
    _prune_empty_parents(data_dir, target)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_storage.py -v`
Expected: 9 passed

Note: the two `rel_path_for` tests assert `2025/09` because epoch `1757260800` is 2025-09-07 UTC. If a test fails on the year, trust the epoch, not the prose.

- [ ] **Step 5: Commit**

```bash
git add server/clipd/storage.py server/tests/test_storage.py
git commit -m "feat: add game-first storage layout with streaming writes"
```

---

### Task 5: Media probing and thumbnails

**Files:**
- Create: `server/clipd/media.py`
- Test: `server/tests/test_media.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ProbeResult` dataclass with `duration_s: float | None`, `width: int | None`, `height: int | None`; `async probe(path: Path) -> ProbeResult`; `async make_thumbnail(src: Path, dest: Path, at_s: float) -> bool`.

This is the *only* ffmpeg work the server does (Global Constraints). `make_thumbnail` returns `False` rather than raising: a missing thumbnail must never fail an ingest, because the watcher would retry a clip that is already safely stored.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_media.py
import json
import pytest
from pathlib import Path
from clipd.media import ProbeResult, probe, make_thumbnail

pytestmark = pytest.mark.asyncio

FFPROBE_JSON = json.dumps({
    "format": {"duration": "90.5"},
    "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
})


class FakeProc:
    def __init__(self, stdout=b"", returncode=0):
        self._stdout, self.returncode = stdout, returncode

    async def communicate(self):
        return self._stdout, b""


async def test_probe_extracts_duration_and_dimensions(monkeypatch, tmp_path):
    async def fake_exec(*args, **kwargs):
        return FakeProc(FFPROBE_JSON.encode())
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = await probe(tmp_path / "a.mp4")
    assert result == ProbeResult(duration_s=90.5, width=1920, height=1080)


async def test_probe_handles_image_without_duration(monkeypatch, tmp_path):
    payload = json.dumps({
        "format": {},
        "streams": [{"codec_type": "video", "width": 2560, "height": 1440}],
    }).encode()

    async def fake_exec(*args, **kwargs):
        return FakeProc(payload)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = await probe(tmp_path / "a.png")
    assert result == ProbeResult(duration_s=None, width=2560, height=1440)


async def test_probe_returns_empty_result_when_ffprobe_fails(monkeypatch, tmp_path):
    async def fake_exec(*args, **kwargs):
        return FakeProc(b"", returncode=1)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await probe(tmp_path / "bad.mp4") == ProbeResult(None, None, None)


async def test_make_thumbnail_reports_success(monkeypatch, tmp_path):
    dest = tmp_path / "thumbs" / "a.jpg"

    async def fake_exec(*args, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpegdata")
        return FakeProc()
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await make_thumbnail(tmp_path / "a.mp4", dest, 9.0) is True
    assert dest.exists()


async def test_make_thumbnail_returns_false_instead_of_raising(monkeypatch, tmp_path):
    # A failed thumbnail must not fail the ingest — the clip is already stored.
    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=1)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await make_thumbnail(tmp_path / "a.mp4", tmp_path / "t.jpg", 1.0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_media.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.media'`

- [ ] **Step 3: Write the implementation**

```python
# server/clipd/media.py
"""ffprobe/ffmpeg wrappers.

The server never transcodes (plan.md §5). The only work here is reading
metadata and extracting a single frame — both CPU-cheap, no GPU, and
specifically NOT QuickSync, which Jellyfin owns (plan.md §10).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float | None
    width: int | None
    height: int | None


EMPTY = ProbeResult(None, None, None)


async def _run(*args: str) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout


async def probe(path: Path) -> ProbeResult:
    code, stdout = await _run(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    )
    if code != 0 or not stdout:
        log.warning("ffprobe failed for %s", path)
        return EMPTY

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("ffprobe returned unparseable json for %s", path)
        return EMPTY

    duration_raw = payload.get("format", {}).get("duration")
    video = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "video"),
        {},
    )
    return ProbeResult(
        duration_s=float(duration_raw) if duration_raw else None,
        width=video.get("width"),
        height=video.get("height"),
    )


async def make_thumbnail(src: Path, dest: Path, at_s: float) -> bool:
    """Extract one frame. Returns False on failure — never raises.

    A thumbnail is cosmetic; the capture it represents is already on disk. If
    this raised, the client would retry an upload that actually succeeded.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, _ = await _run(
        "ffmpeg", "-y", "-ss", f"{at_s:.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(dest),
    )
    if code != 0:
        log.warning("thumbnail generation failed for %s", src)
        return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_media.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add server/clipd/media.py server/tests/test_media.py
git commit -m "feat: add ffprobe metadata and thumbnail extraction"
```

---

### Task 6: ntfy notifications

**Files:**
- Create: `server/clipd/notify.py`
- Test: `server/tests/test_notify.py`

**Interfaces:**
- Consumes: `clipd.config.Config`
- Produces: `async notify_capture(cfg: Config, *, title: str, body: str, click_url: str) -> bool`; `async notify_retention(cfg: Config, *, title: str, body: str, priority: str = "default") -> bool`; `human_bytes(n: int) -> str`.

`plan.md` §4 requires ntfy with one topic per service, and §6 requires the click action to be the clip URL.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_notify.py
import pytest
from clipd.config import Config
from clipd.notify import human_bytes, notify_capture, notify_retention

pytestmark = pytest.mark.asyncio


def cfg(topic="clipd-test"):
    return Config.from_env({"INGEST_TOKEN": "t", "NTFY_TOPIC": topic or ""})


@pytest.mark.parametrize("raw,expected", [
    (512, "512 B"), (1536, "1.5 KB"), (5 * 1024**2, "5.0 MB"),
    (int(1.5 * 1024**3), "1.5 GB"),
])
def test_human_bytes_formats_sizes(raw, expected):
    assert human_bytes(raw) == expected


async def test_notify_capture_posts_to_topic_with_click_action(monkeypatch):
    sent = {}

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            sent.update(url=url, content=content, headers=headers)
            class R:
                status_code = 200
                def raise_for_status(self): pass
            return R()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

    ok = await notify_capture(
        cfg(), title="Counter-Strike 2", body="90s · 142.0 MB",
        click_url="http://midget:8000/v/aB3xY9z",
    )

    assert ok is True
    assert sent["url"] == "https://ntfy.sh/clipd-test"
    assert sent["content"] == "90s · 142.0 MB"
    assert sent["headers"]["Title"] == "Counter-Strike 2"
    assert sent["headers"]["Click"] == "http://midget:8000/v/aB3xY9z"


async def test_notify_is_a_no_op_when_topic_unset(monkeypatch):
    def explode(**kw):
        raise AssertionError("must not build a client without a topic")
    monkeypatch.setattr("httpx.AsyncClient", explode)

    assert await notify_capture(cfg(topic=None), title="t", body="b", click_url="u") is False


async def test_notify_swallows_transport_errors(monkeypatch):
    # ntfy.sh being unreachable must never fail an ingest.
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise OSError("dns failure")

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

    assert await notify_capture(cfg(), title="t", body="b", click_url="u") is False


async def test_notify_retention_sets_priority_header(monkeypatch):
    sent = {}

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            sent.update(headers=headers)
            class R:
                status_code = 200
                def raise_for_status(self): pass
            return R()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

    await notify_retention(cfg(), title="Store full", body="over budget", priority="high")
    assert sent["headers"]["Priority"] == "high"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_notify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.notify'`

- [ ] **Step 3: Write the implementation**

```python
# server/clipd/notify.py
"""ntfy push. One topic per service, per plan.md §4."""
from __future__ import annotations

import logging

import httpx

from .config import Config

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


async def _post(cfg: Config, headers: dict[str, str], body: str) -> bool:
    if not cfg.ntfy_topic:
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(
                f"{cfg.ntfy_server}/{cfg.ntfy_topic}",
                content=body,
                headers=headers,
            )
            response.raise_for_status()
        return True
    except Exception:
        # Never propagate: a push failure must not fail the work that triggered it.
        log.warning("ntfy push failed", exc_info=True)
        return False


async def notify_capture(cfg: Config, *, title: str, body: str, click_url: str) -> bool:
    return await _post(
        cfg,
        {"Title": title, "Click": click_url, "Tags": "clapper"},
        body,
    )


async def notify_retention(
    cfg: Config, *, title: str, body: str, priority: str = "default"
) -> bool:
    return await _post(
        cfg,
        {"Title": title, "Priority": priority, "Tags": "floppy_disk"},
        body,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_notify.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add server/clipd/notify.py server/tests/test_notify.py
git commit -m "feat: add ntfy push with clip url as click action"
```

---

### Task 7: The `/ingest` endpoint

**Files:**
- Create: `server/clipd/app.py`
- Test: `server/tests/test_ingest.py`

**Interfaces:**
- Consumes: `config.Config`, `db.*`, `storage.*`, `media.*`, `notify.notify_capture`, `ids.new_id`, `slug.slugify_game`
- Produces: `create_app(cfg: Config) -> FastAPI`; `IngestMeta` pydantic model; `require_ingest_token` dependency. `app.state.cfg` and `app.state.conn` are set during lifespan and relied on by Tasks 8 and 9.

This is where idempotency lives. Spec §3: the watcher retries and only deletes its local file after a confirmed 200, so a successful POST with a lost response must return the *same* id rather than ingest twice.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ingest.py
import json
import pytest
from fastapi.testclient import TestClient

from clipd.app import create_app
from clipd.config import Config
from clipd.media import ProbeResult


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env({
        "INGEST_TOKEN": "secret-token",
        "DATA_DIR": str(tmp_path),
        "BASE_URL": "http://midget:8000",
        "SWEEP_INTERVAL_S": "0",   # no background sweep during tests
    })


@pytest.fixture
def client(cfg, monkeypatch):
    async def fake_probe(path):
        return ProbeResult(duration_s=90.0, width=1920, height=1080)

    async def fake_thumb(src, dest, at_s):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr("clipd.app.media.probe", fake_probe)
    monkeypatch.setattr("clipd.app.media.make_thumbnail", fake_thumb)
    monkeypatch.setattr("clipd.app.notify_capture", lambda *a, **kw: _true())

    with TestClient(create_app(cfg)) as c:
        yield c


async def _true():
    return True


def post_clip(client, *, uuid="u-1", game="Counter-Strike 2", token="secret-token",
              kind="clip", filename="replay.mp4", body=b"videodata"):
    meta = {
        "kind": kind, "capture_uuid": uuid, "game": game,
        "game_exe": "cs2.exe", "source_host": "desktop-amtr56i",
        "captured_at": 1757260800,
    }
    return client.post(
        "/ingest",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, body, "video/mp4")},
        data={"meta": json.dumps(meta)},
    )


def test_ingest_stores_file_under_game_and_date(client, cfg):
    response = post_clip(client)
    assert response.status_code == 200

    clip_id = response.json()["id"]
    stored = cfg.data_dir / "clips" / "counter-strike-2" / "2025" / "09" / f"{clip_id}.mp4"
    assert stored.read_bytes() == b"videodata"


def test_ingest_returns_id_and_url(client):
    payload = post_clip(client).json()
    assert len(payload["id"]) == 7
    assert payload["url"] == f"http://midget:8000/v/{payload['id']}"


def test_ingest_writes_thumbnail(client, cfg):
    clip_id = post_clip(client).json()["id"]
    assert (cfg.data_dir / "thumbs" / f"{clip_id}.jpg").exists()


def test_ingest_records_probed_metadata(client, cfg):
    from clipd import db
    clip_id = post_clip(client).json()["id"]
    conn = db.connect(cfg.data_dir / "clipd.db")
    clip = db.get_by_id(conn, clip_id)
    assert (clip.duration_s, clip.width, clip.height) == (90.0, 1920, 1080)
    assert clip.game == "Counter-Strike 2"
    assert clip.game_slug == "counter-strike-2"
    assert clip.game_exe == "cs2.exe"
    assert clip.bytes == len(b"videodata")


def test_repeated_capture_uuid_returns_same_id_without_second_file(client, cfg):
    first = post_clip(client, uuid="same").json()
    second = post_clip(client, uuid="same").json()

    assert first["id"] == second["id"]
    assert second["duplicate"] is True
    stored = list((cfg.data_dir / "clips").rglob("*.mp4"))
    assert len(stored) == 1


def test_missing_game_files_under_unknown(client, cfg):
    clip_id = post_clip(client, uuid="u-2", game=None).json()["id"]
    assert (cfg.data_dir / "clips" / "unknown" / "2025" / "09" / f"{clip_id}.mp4").exists()


def test_screenshot_goes_to_shots_root(client, cfg):
    clip_id = post_clip(
        client, uuid="u-3", kind="screenshot", filename="shot.png", body=b"pngdata"
    ).json()["id"]
    assert (cfg.data_dir / "shots" / "counter-strike-2" / "2025" / "09" / f"{clip_id}.png").exists()


def test_rejects_missing_token(client):
    response = client.post("/ingest", files={"file": ("a.mp4", b"x", "video/mp4")},
                           data={"meta": "{}"})
    assert response.status_code == 401


def test_rejects_wrong_token(client):
    assert post_clip(client, token="wrong-token").status_code == 401


def test_rejects_unknown_kind(client):
    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret-token"},
        files={"file": ("a.mp4", b"x", "video/mp4")},
        data={"meta": json.dumps({"kind": "video", "capture_uuid": "u",
                                  "source_host": "h"})},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.app'`

- [ ] **Step 3: Write the implementation**

```python
# server/clipd/app.py
"""FastAPI application: routes, auth, and lifespan wiring."""
from __future__ import annotations

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
        yield
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

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_ingest.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add server/clipd/app.py server/tests/test_ingest.py
git commit -m "feat: add idempotent /ingest endpoint with bearer auth"
```

---

### Task 8: The `/healthz` endpoint

**Files:**
- Modify: `server/clipd/app.py` — add a route inside `create_app`, after `ingest`
- Test: `server/tests/test_healthz.py`

**Interfaces:**
- Consumes: `db.store_totals`, `app.state.conn`, `app.state.cfg`
- Produces: `GET /healthz` returning `{"status": "ok", "clips": int, "bytes": int, "budget_bytes": int, "used_pct": float}`

`plan.md` §10 specifies an Uptime Kuma HTTP check against `/healthz`. It is deliberately unauthenticated — it exposes only counts, and it is tailnet-only anyway.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_healthz.py
import json
import pytest
from fastapi.testclient import TestClient

from clipd.app import create_app
from clipd.config import Config
from clipd.media import ProbeResult


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env({
        "INGEST_TOKEN": "secret-token",
        "DATA_DIR": str(tmp_path),
        "MAX_STORE_BYTES": "1000",
        "SWEEP_INTERVAL_S": "0",
    })


@pytest.fixture
def client(cfg, monkeypatch):
    async def fake_probe(path):
        return ProbeResult(90.0, 1920, 1080)

    async def fake_thumb(src, dest, at_s):
        return False

    async def fake_notify(*a, **kw):
        return True

    monkeypatch.setattr("clipd.app.media.probe", fake_probe)
    monkeypatch.setattr("clipd.app.media.make_thumbnail", fake_thumb)
    monkeypatch.setattr("clipd.app.notify_capture", fake_notify)

    with TestClient(create_app(cfg)) as c:
        yield c


def test_healthz_reports_ok_on_empty_store(client):
    payload = client.get("/healthz").json()
    assert payload == {"status": "ok", "clips": 0, "bytes": 0,
                       "budget_bytes": 1000, "used_pct": 0.0}


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").status_code == 200


def test_healthz_counts_ingested_clips(client):
    meta = {"kind": "clip", "capture_uuid": "u-1", "game": "Halo",
            "source_host": "h", "captured_at": 1757260800}
    client.post("/ingest", headers={"Authorization": "Bearer secret-token"},
                files={"file": ("a.mp4", b"x" * 250, "video/mp4")},
                data={"meta": json.dumps(meta)})

    payload = client.get("/healthz").json()
    assert payload["clips"] == 1
    assert payload["bytes"] == 250
    assert payload["used_pct"] == 25.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_healthz.py -v`
Expected: FAIL — 404 Not Found, since `/healthz` is not registered

- [ ] **Step 3: Add the route**

Insert inside `create_app`, immediately after the `ingest` function and before `return app`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_healthz.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add server/clipd/app.py server/tests/test_healthz.py
git commit -m "feat: add /healthz endpoint for uptime kuma"
```

---

### Task 9: Retention sweep

**Files:**
- Create: `server/clipd/retention.py`
- Modify: `server/clipd/app.py` — start the sweep task in `lifespan`
- Test: `server/tests/test_retention.py`

**Interfaces:**
- Consumes: `db.store_totals`, `db.prunable_oldest_first`, `db.unprunable_bytes`, `db.delete_clip`, `storage.remove_capture`, `notify.notify_retention`, `config.Config`
- Produces: `SweepResult` dataclass with `deleted: int`, `freed_bytes: int`, `total_bytes: int`, `unprunable_bytes: int`, `over_budget: bool`; `RetentionState` dataclass with `warned_high: bool`, `warned_unprunable: bool`; `sweep(conn, cfg) -> SweepResult`; `async sweep_and_notify(conn, cfg, state: RetentionState) -> SweepResult`; `async sweep_loop(conn, cfg, state) -> None`.

Spec §7 is the binding requirement here, including the unprunable case: alert loudly, **keep accepting uploads**.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_retention.py
import pytest
from clipd import db, retention
from clipd.config import Config
from clipd.retention import RetentionState, sweep, sweep_and_notify


def make_clip(clip_id, created_at, size, *, pinned=0, public_slug=None):
    return db.Clip(
        id=clip_id, public_slug=public_slug, capture_uuid=f"u-{clip_id}",
        kind="clip", title=None, filename=f"{clip_id}.mp4",
        rel_path=f"clips/halo/2026/09/{clip_id}.mp4", bytes=size,
        duration_s=90.0, width=1920, height=1080, game="Halo",
        game_slug="halo", game_exe="halo.exe", pinned=pinned,
        source_host="h", created_at=created_at,
        thumb_path=f"thumbs/{clip_id}.jpg",
    )


@pytest.fixture
def store(tmp_path):
    cfg = Config.from_env({
        "INGEST_TOKEN": "t", "DATA_DIR": str(tmp_path), "MAX_STORE_BYTES": "1000",
    })
    conn = db.connect(tmp_path / "clipd.db")
    db.init_schema(conn)
    yield conn, cfg
    conn.close()


def add(conn, cfg, clip):
    db.insert_clip(conn, clip)
    target = cfg.data_dir / clip.rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * clip.bytes)


def test_sweep_does_nothing_under_budget(store):
    conn, cfg = store
    add(conn, cfg, make_clip("a", 100, 400))

    result = sweep(conn, cfg)

    assert result.deleted == 0
    assert result.over_budget is False
    assert db.get_by_id(conn, "a") is not None


def test_sweep_deletes_oldest_first_until_under_budget(store):
    conn, cfg = store
    add(conn, cfg, make_clip("oldest", 100, 500))
    add(conn, cfg, make_clip("middle", 200, 500))
    add(conn, cfg, make_clip("newest", 300, 500))

    result = sweep(conn, cfg)  # 1500 bytes against a 1000 budget

    assert result.deleted == 1
    assert result.freed_bytes == 500
    assert db.get_by_id(conn, "oldest") is None
    assert db.get_by_id(conn, "middle") is not None
    assert db.get_by_id(conn, "newest") is not None


def test_sweep_removes_the_file_and_thumbnail(store):
    conn, cfg = store
    add(conn, cfg, make_clip("a", 100, 900))
    add(conn, cfg, make_clip("b", 200, 900))
    thumb = cfg.data_dir / "thumbs" / "a.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpeg")

    sweep(conn, cfg)

    assert not (cfg.data_dir / "clips" / "halo" / "2026" / "09" / "a.mp4").exists()
    assert not thumb.exists()


def test_sweep_never_deletes_pinned_or_shared(store):
    conn, cfg = store
    add(conn, cfg, make_clip("pinned", 100, 800, pinned=1))
    add(conn, cfg, make_clip("shared", 200, 800, public_slug="slugslugslugslugslug12"))

    result = sweep(conn, cfg)  # 1600 bytes against a 1000 budget

    assert result.deleted == 0
    assert result.over_budget is True
    assert result.unprunable_bytes == 1600
    assert db.get_by_id(conn, "pinned") is not None
    assert db.get_by_id(conn, "shared") is not None


def test_sweep_prunes_around_pinned_clips_to_get_under_budget(store):
    conn, cfg = store
    add(conn, cfg, make_clip("prunable", 100, 300))
    add(conn, cfg, make_clip("pinned", 200, 900, pinned=1))

    result = sweep(conn, cfg)  # 1200 bytes against a 1000 budget

    assert result.deleted == 1
    assert result.total_bytes == 900
    assert result.over_budget is False
    assert db.get_by_id(conn, "pinned") is not None


def test_sweep_stays_over_budget_when_protected_clips_exceed_it(store):
    conn, cfg = store
    add(conn, cfg, make_clip("prunable", 100, 200))
    add(conn, cfg, make_clip("pinned", 200, 1500, pinned=1))

    result = sweep(conn, cfg)  # 1700 bytes against a 1000 budget

    assert result.deleted == 1          # took everything it was allowed to
    assert result.total_bytes == 1500
    assert result.over_budget is True   # and is still over — this is the alert case


async def test_sweep_and_notify_warns_once_at_eighty_percent(store, monkeypatch):
    conn, cfg = store
    sent = []

    async def fake_notify(config, *, title, body, priority="default"):
        sent.append((title, priority))
        return True

    monkeypatch.setattr("clipd.retention.notify_retention", fake_notify)
    add(conn, cfg, make_clip("a", 100, 850))

    state = RetentionState()
    await sweep_and_notify(conn, cfg, state)
    await sweep_and_notify(conn, cfg, state)  # still high; must not re-warn

    assert len(sent) == 1
    assert "80%" in sent[0][0] or "budget" in sent[0][0].lower()


async def test_sweep_and_notify_sends_high_priority_alert_when_unprunable(store, monkeypatch):
    conn, cfg = store
    sent = []

    async def fake_notify(config, *, title, body, priority="default"):
        sent.append((title, body, priority))
        return True

    monkeypatch.setattr("clipd.retention.notify_retention", fake_notify)
    add(conn, cfg, make_clip("pinned", 100, 1500, pinned=1))

    await sweep_and_notify(conn, cfg, RetentionState())

    assert any(p == "high" for _, _, p in sent)
    assert any("unprunable" in b.lower() or "pinned" in b.lower() for _, b, _ in sent)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_retention.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipd.retention'`

- [ ] **Step 3: Write the implementation**

```python
# server/clipd/retention.py
"""Size-capped retention. No age limit — spec §7.

Radarr and Sonarr consume the same disk (plan.md §6), so clips must not be
the thing that fills it at 3am.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import db, storage
from .config import Config
from .notify import human_bytes, notify_retention

log = logging.getLogger(__name__)

WARN_THRESHOLD = 0.80  # plan.md §6: warn at ~80% of budget


@dataclass(frozen=True)
class SweepResult:
    deleted: int
    freed_bytes: int
    total_bytes: int
    unprunable_bytes: int
    over_budget: bool


@dataclass
class RetentionState:
    """Edge-triggers the alerts so a full store does not push every 15 minutes."""
    warned_high: bool = False
    warned_unprunable: bool = False


def sweep(conn, cfg: Config) -> SweepResult:
    _, total = db.store_totals(conn)
    budget = cfg.max_store_bytes
    deleted = freed = 0

    if total > budget:
        for clip in db.prunable_oldest_first(conn):
            if total <= budget:
                break
            storage.remove_capture(cfg.data_dir, clip.rel_path)
            if clip.thumb_path:
                storage.remove_capture(cfg.data_dir, clip.thumb_path)
            db.delete_clip(conn, clip.id)
            total -= clip.bytes
            freed += clip.bytes
            deleted += 1
            log.info("retention: deleted %s (%s, %s)",
                     clip.id, clip.game_slug, human_bytes(clip.bytes))

    return SweepResult(
        deleted=deleted,
        freed_bytes=freed,
        total_bytes=total,
        unprunable_bytes=db.unprunable_bytes(conn),
        over_budget=total > budget,
    )


async def sweep_and_notify(conn, cfg: Config, state: RetentionState) -> SweepResult:
    result = sweep(conn, cfg)
    budget = cfg.max_store_bytes

    if result.over_budget:
        # Nothing left to prune: everything above budget is pinned or shared.
        # Shout, but keep accepting uploads — plan.md §8.7 guarantees no capture
        # is lost, and dropping one is worse than a temporarily oversized store.
        if not state.warned_unprunable:
            await notify_retention(
                cfg,
                title="clipd store over budget",
                body=(
                    f"{human_bytes(result.total_bytes)} of {human_bytes(budget)} used. "
                    f"{human_bytes(result.unprunable_bytes)} is unprunable "
                    f"(pinned or shared). Still accepting uploads."
                ),
                priority="high",
            )
            state.warned_unprunable = True
    else:
        state.warned_unprunable = False

    ratio = result.total_bytes / budget if budget else 0.0
    if ratio >= WARN_THRESHOLD and not result.over_budget:
        if not state.warned_high:
            await notify_retention(
                cfg,
                title=f"clipd store at {ratio:.0%} of budget",
                body=f"{human_bytes(result.total_bytes)} of {human_bytes(budget)} used.",
            )
            state.warned_high = True
    elif ratio < WARN_THRESHOLD:
        state.warned_high = False

    return result


async def sweep_loop(conn, cfg: Config, state: RetentionState) -> None:
    while True:
        await asyncio.sleep(cfg.sweep_interval_s)
        try:
            await sweep_and_notify(conn, cfg, state)
        except Exception:
            log.exception("retention sweep failed; will retry next interval")
```

- [ ] **Step 4: Wire the loop into the app lifespan**

In `server/clipd/app.py`, add `import asyncio` and `from .retention import RetentionState, sweep_loop`, then replace the `lifespan` body with:

```python
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
```

- [ ] **Step 5: Run the full suite**

Run: `cd server && python -m pytest -v`
Expected: all tests pass across every test file

- [ ] **Step 6: Commit**

```bash
git add server/clipd/retention.py server/clipd/app.py server/tests/test_retention.py
git commit -m "feat: add size-capped retention sweep with edge-triggered alerts"
```

---

### Task 10: Containerize and deploy to midget

**Files:**
- Create: `server/Dockerfile`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `README.md`

**Interfaces:**
- Consumes: everything above
- Produces: a running service at `http://midget.tail0056d7.ts.net:8000`

Requires SSH key auth to `batman@100.72.75.62` (see Prerequisite).

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
# server/Dockerfile
FROM python:3.12-slim

# ffmpeg is required for thumbnails and (later) -c copy trims. This is the
# ONLY reason the image is not python:3.12-alpine — ffmpeg on Alpine is a
# fight not worth having for ~40MB.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY clipd ./clipd
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "clipd.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Add the ASGI entrypoint**

`uvicorn` needs a module-level app object, but `create_app` takes a Config. Create `server/clipd/asgi.py`:

```python
# server/clipd/asgi.py
"""Production entrypoint: builds the app from the process environment."""
import logging
import os

from .app import create_app
from .config import Config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app(Config.from_env(os.environ))
```

- [ ] **Step 3: Write the compose file, env example, and gitignore**

```yaml
# docker-compose.yml
services:
  clipd:
    build: ./server
    container_name: clipd
    restart: unless-stopped
    ports:
      # 8000 is free on midget; 3000/3001/5055/6767/7878/8080/8096/8989/9696
      # are all taken by existing services. See plan.md §3.
      - "8000:8000"
    volumes:
      # Bind mount, not a named volume — house convention (plan.md §4), and it
      # keeps the clip store visible to normal filesystem tools.
      - ./data:/data
    environment:
      TZ: "America/Chicago"
      DATA_DIR: "/data"
      INGEST_TOKEN: "${INGEST_TOKEN:?INGEST_TOKEN must be set in .env}"
      NTFY_TOPIC: "${NTFY_TOPIC}"
      MAX_STORE_BYTES: "${MAX_STORE_BYTES:-50GB}"
      BASE_URL: "${BASE_URL}"
    # No Docker socket mount: clipd has no reason to talk to the daemon, and a
    # read-only socket is still effectively root on the host (plan.md §4).
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```

```bash
# .env.example  — copy to .env and fill in. NEVER commit .env.
# Generate with: openssl rand -base64 32
INGEST_TOKEN=replace-me

# One ntfy topic per service (plan.md §4). Use something unguessable —
# ntfy.sh topics are public to anyone who knows the name.
NTFY_TOPIC=clipd-replace-me

# Size cap only, no age limit (spec §7).
MAX_STORE_BYTES=50GB

# Used to build the URLs returned by /ingest and pushed to ntfy.
BASE_URL=http://midget.tail0056d7.ts.net:8000
```

```
# .gitignore
.env
data/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
.DS_Store
```

- [ ] **Step 4: Verify the build locally before touching midget**

```bash
cd /Users/ruhanmalik/Desktop/Workplace/clip
cp .env.example .env
sed -i '' "s|^INGEST_TOKEN=.*|INGEST_TOKEN=$(openssl rand -base64 32)|" .env
sed -i '' "s|^BASE_URL=.*|BASE_URL=http://localhost:8000|" .env
docker compose up --build -d
sleep 5
curl -fsS http://localhost:8000/healthz
```

Expected: `{"status":"ok","clips":0,"bytes":0,"budget_bytes":53687091200,"used_pct":0.0}`

- [ ] **Step 5: Exercise a real ingest locally**

```bash
cd /Users/ruhanmalik/Desktop/Workplace/clip
TOKEN=$(grep '^INGEST_TOKEN=' .env | cut -d= -f2-)
# A real 3-second H.264 file, so ffprobe and the thumbnail path are exercised.
ffmpeg -y -f lavfi -i testsrc=size=1920x1080:rate=30 -t 3 \
       -c:v libx264 -pix_fmt yuv420p -movflags +faststart /tmp/test.mp4

curl -sS -H "Authorization: Bearer $TOKEN" \
     -F file=@/tmp/test.mp4 \
     -F 'meta={"kind":"clip","capture_uuid":"local-test-1","game":"Counter-Strike 2","game_exe":"cs2.exe","source_host":"macbook-pro-3"}' \
     http://localhost:8000/ingest
```

Expected: `{"id":"<7 chars>","url":"http://localhost:8000/v/<id>"}`

Then confirm every Step 1 acceptance criterion from spec §9:

```bash
ID=$(curl -sS -H "Authorization: Bearer $TOKEN" -F file=@/tmp/test.mp4 \
  -F 'meta={"kind":"clip","capture_uuid":"local-test-2","game":"Counter-Strike 2","source_host":"macbook-pro-3"}' \
  http://localhost:8000/ingest | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

ls -la data/clips/counter-strike-2/*/*/"$ID".mp4   # file at the game-first path
ls -la data/thumbs/"$ID".jpg                        # thumbnail exists
curl -sS http://localhost:8000/healthz              # counts are accurate

# Idempotency: same capture_uuid returns the same id, no second file.
curl -sS -H "Authorization: Bearer $TOKEN" -F file=@/tmp/test.mp4 \
  -F 'meta={"kind":"clip","capture_uuid":"local-test-2","game":"Counter-Strike 2","source_host":"macbook-pro-3"}' \
  http://localhost:8000/ingest
find data/clips -name '*.mp4' | wc -l               # must not have grown
```

Expected: the file and thumbnail exist, `/healthz` counts match, and the repeated
`capture_uuid` returns `"duplicate": true` with the same `id` and no new file.

- [ ] **Step 6: Commit**

```bash
git add server/Dockerfile server/clipd/asgi.py docker-compose.yml .env.example .gitignore
git commit -m "feat: containerize clipd with compose and healthcheck"
```

- [ ] **Step 7: Deploy to midget**

```bash
ssh batman@100.72.75.62 'mkdir -p /home/batman/clipd'
cd /Users/ruhanmalik/Desktop/Workplace/clip
rsync -av --exclude data --exclude .env --exclude .git --exclude __pycache__ \
      ./ batman@100.72.75.62:/home/batman/clipd/

ssh batman@100.72.75.62 'cd /home/batman/clipd && cp .env.example .env && \
  sed -i "s|^INGEST_TOKEN=.*|INGEST_TOKEN=$(openssl rand -base64 32)|" .env && \
  sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=clipd-$(openssl rand -hex 8)|" .env && \
  docker compose up --build -d'
```

- [ ] **Step 8: Verify the deployment from the MacBook**

```bash
curl -fsS http://midget.tail0056d7.ts.net:8000/healthz
ssh batman@100.72.75.62 'cd /home/batman/clipd && docker compose ps && docker compose logs --tail 30'
```

Expected: `/healthz` returns over the tailnet, the container reports `healthy`,
and the logs show no errors. Subscribe to the `NTFY_TOPIC` from `.env` on the
phone, run the Step 5 ingest against `midget.tail0056d7.ts.net`, and confirm the
push arrives with the clip URL as its click action.

- [ ] **Step 9: Write the README and commit**

```markdown
# clipd

Self-hosted clip and screenshot capture. See `plan.md` for the full design and
`docs/superpowers/specs/` for the current spec.

## Layout
- `server/` — the FastAPI service
- `docker-compose.yml` — deploys to `/home/batman/clipd/` on midget
- `.env.example` — copy to `.env`, fill in, never commit

## Development
    cd server && pip install -e ".[dev]" && python -m pytest

## Deploy
    rsync -av --exclude data --exclude .env ./ batman@100.72.75.62:/home/batman/clipd/
    ssh batman@100.72.75.62 'cd /home/batman/clipd && docker compose up --build -d'
```

```bash
git add README.md
git commit -m "docs: add clipd readme"
```

---

## What this plan does not cover

Deliberately out of scope, each with its own follow-on plan:

- **Plan B — Windows watcher.** OBS replay-buffer config, the fsnotify watcher, the
  exe ring-buffer game detection from spec §5, `games.toml`, clipboard, retry queue.
  Note: spec §4 shows `games.toml` in the server directory, but detection is entirely
  client-side. Plan B decides whether the watcher ships it locally or fetches it from
  clipd; Task 10 above deliberately does not create it.
- **Plan C — web app / PWA.** Game-first landing, `/v/<id>`, bulk delete, pin, rename,
  download, trim, and the dormant `/c/<slug>` share routes.
- **Plan D — homelab integration.** Homepage tile (requires updating
  `HOMEPAGE_ALLOWED_HOSTS`, `plan.md` §10), Uptime Kuma monitor against `/healthz`.

## Self-review notes

- **Spec coverage:** §2 stack → Tasks 1, 10. §3 schema → Task 3 (all four new columns).
  §4 layout and slugging → Tasks 2, 4. §5 detection → deferred to Plan B (client-side).
  §6 `/ingest` and `/healthz` → Tasks 7, 8; remaining routes are Plan C. §7 retention
  including the unprunable case → Task 9. §8 web app → Plan C. §9 definition of done →
  Task 10 Steps 5 and 8. §11 SSH prerequisite → stated at the top.
- **Placeholder scan:** clean — no TBDs, and every code step carries real code.
- **Type consistency:** `Clip` field names are identical across Tasks 3, 7, and 9;
  `SweepResult` fields match between the Task 9 implementation and its tests;
  `ProbeResult` matches between Tasks 5 and 7.
- **Fixed during review:** an earlier draft of Task 9 asserted `over_budget is True`
  on a store that pruning had brought *under* budget. Split into two tests — one
  where pruning around a pinned clip succeeds, one where protected clips alone
  exceed the budget and the alert must fire.
- **Epoch note:** tests use `captured_at = 1757260800`, which is 2025-09-07 UTC, so
  path assertions read `2025/09`. Deliberate and internally consistent; trust the
  epoch over the calendar year when reading those tests.
