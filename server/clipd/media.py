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
