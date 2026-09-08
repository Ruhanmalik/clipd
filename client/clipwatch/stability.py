"""Wait for a capture to finish being written.

plan.md §8.2: OBS is still flushing the replay buffer when the first
filesystem event arrives, so the file must be seen to stop growing before it
is remuxed or uploaded. Clock functions are injected so tests do not sleep.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def wait_until_stable(
    path: Path,
    *,
    checks: int,
    interval_s: float,
    timeout_s: float,
    sleep=time.sleep,
    now=time.monotonic,
) -> bool:
    """True once the file has held the same non-zero size for `checks` polls."""
    deadline = now() + timeout_s
    last = _size(path)
    stable = 0

    while now() < deadline:
        sleep(interval_s)
        current = _size(path)

        if current is None:
            log.debug("%s vanished while waiting", path)
            return False

        if current == last and current > 0:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0

        last = current

    log.warning("%s never stopped changing within %ss", path, timeout_s)
    return False
