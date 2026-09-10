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


@dataclass(frozen=True)
class GameSummary:
    """One tile on the landing page."""
    game_slug: str
    game: str | None
    clips: int
    bytes: int
    latest_at: int
    cover_id: str | None
    cover_thumb: str | None


def _to_clips(cur: sqlite3.Cursor) -> list[Clip]:
    return [c for c in (_to_clip(r) for r in cur.fetchall()) if c is not None]


def list_games(conn: sqlite3.Connection) -> list[GameSummary]:
    """One row per game, newest activity first.

    `game`, `id`, and `thumb_path` are bare columns beside MAX(created_at).
    SQLite documents these as taking their values from the row that produced
    the maximum, which is exactly the cover we want — the newest capture —
    without a correlated subquery per game.
    """
    cur = conn.execute("""
        SELECT game_slug,
               game,
               id                       AS cover_id,
               thumb_path               AS cover_thumb,
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
            cover_thumb=row["cover_thumb"],
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


def store_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM clips")
    count, total = cur.fetchone()
    return int(count), int(total)


def prunable_oldest_first(conn: sqlite3.Connection) -> list[Clip]:
    cur = conn.execute(
        f"SELECT * FROM clips WHERE NOT {_PROTECTED} ORDER BY created_at ASC"
    )
    return _to_clips(cur)


def unprunable_bytes(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        f"SELECT COALESCE(SUM(bytes), 0) FROM clips WHERE {_PROTECTED}"
    )
    return int(cur.fetchone()[0])


def delete_clip(conn: sqlite3.Connection, clip_id: str) -> None:
    conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
    conn.commit()
