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


def needs_remux(path: Path) -> bool:
    return path.suffix.lower() == ".mkv"


def remux_to_mp4(src: Path, dest: Path, timeout_s: float = 300) -> bool:
    """Container swap only. Returns False on failure and leaves no partial file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
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
