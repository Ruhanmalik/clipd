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

# A truncated or malformed capture can wedge ffprobe indefinitely. The request
# holds its spooled upload while it waits, so a few of these would exhaust the
# single worker on midget's i7-7700HQ.
TIMEOUT_S = 60.0

FAILED = (-1, b"")


async def _run(*args: str) -> tuple[int, bytes]:
    """Run a subprocess to completion. Never raises — returns FAILED instead.

    Callers treat ffmpeg/ffprobe as best-effort. Letting a missing binary or a
    fork failure propagate would 500 an ingest whose file is already on disk.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        return proc.returncode, stdout
    except asyncio.TimeoutError:
        log.warning("%s timed out after %ss", args[0], TIMEOUT_S)
        if proc is not None:
            proc.kill()
        return FAILED
    except OSError:
        # Binary missing from the image, PATH changed, or fork failed under
        # memory pressure. All three are the caller's "it did not work" case.
        log.warning("could not run %s", args[0], exc_info=True)
        return FAILED


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
