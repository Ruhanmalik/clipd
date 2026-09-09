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
    old = resolve_capture(data_dir, old_rel)
    new = resolve_capture(data_dir, new_rel)
    new.parent.mkdir(parents=True, exist_ok=True)
    old.replace(new)
    _prune_empty_parents(data_dir, old)


def remove_capture(data_dir: Path, rel_path: str) -> None:
    target = resolve_capture(data_dir, rel_path)
    target.unlink(missing_ok=True)
    _prune_empty_parents(data_dir, target)
