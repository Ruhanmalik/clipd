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
