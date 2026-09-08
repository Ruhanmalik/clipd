"""MKV -> faststart MP4, without re-encoding.

OBS records MKV because it survives a crash; browsers cannot play MKV. The
conversion is a container swap (`-c copy`), so it is lossless and sub-second
on the 7900X — and it happens here rather than on the server, which must
never transcode (plan.md §5).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

KIND_FOR_SUFFIX = {
    ".mkv": "clip",
    ".mp4": "clip",
    ".mov": "clip",
    ".png": "screenshot",
    ".jpg": "screenshot",
    ".jpeg": "screenshot",
}


CLIP_SUFFIXES = {s for s, kind in KIND_FOR_SUFFIX.items() if kind == "clip"}


def needs_remux(path: Path) -> bool:
    """Every clip container goes through the remux, not just MKV.

    plan.md §10 makes +faststart mandatory, and an OBS set to record MP4 or
    MOV would otherwise upload a file with its moov atom at the end. The pass
    is `-c copy`, so for an already-faststart file it costs a file copy.
    """
    return path.suffix.lower() in CLIP_SUFFIXES


def remux_to_mp4(src: Path, dest: Path, timeout_s: float = 300) -> bool:
    """Container swap only. Returns False on failure and leaves no partial file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
                # -map 0 keeps every stream. Without it ffmpeg selects one per
                # type, silently discarding OBS's extra audio tracks (game-only,
                # mic-only) while still reporting success.
                "-map", "0",
                "-ignore_unknown",          # attachments/data must not fail it
                "-c", "copy",               # never re-encode
                "-movflags", "+faststart",  # plan.md §10, mandatory
                str(dest),
            ],
            capture_output=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("remux failed to run for %s", src, exc_info=True)
        dest.unlink(missing_ok=True)
        return False

    if result.returncode != 0:
        log.warning("remux failed for %s: %s", src,
                    result.stderr.decode("utf-8", "replace")[:400])
        dest.unlink(missing_ok=True)
        return False

    return True
