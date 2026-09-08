"""Which game was being played over the capture window.

design §5: the window title at hotkey time is unreliable — alt-tabbing to
Discord before hitting the hotkey would file the clip under "Discord". So we
sample the foreground process executable on a timer and take the most common
non-ignored one across the replay-buffer window.

Time is injected rather than read from the clock, so the rule is testable.
"""
from __future__ import annotations

from collections import deque


class ExeRingBuffer:
    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._samples: deque[tuple[float, str]] = deque()
        self.titles: dict[str, str] = {}

    def record(self, exe: str | None, now: float, title: str | None = None) -> None:
        if not exe:
            return
        key = exe.lower()
        self._samples.append((now, key))
        if title:
            self.titles[key] = title
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def dominant(self, ignore: frozenset[str], now: float) -> str | None:
        self._evict(now)

        counts: dict[str, int] = {}
        last_seen: dict[str, float] = {}
        for at, exe in self._samples:
            if exe in ignore:
                continue
            counts[exe] = counts.get(exe, 0) + 1
            last_seen[exe] = at

        if not counts:
            return None
        # Ties break toward whatever was in front most recently.
        return max(counts, key=lambda e: (counts[e], last_seen[e]))


def record_sample(buffer: ExeRingBuffer, adapter, now: float) -> None:
    """Take one foreground sample from a PlatformAdapter into the buffer."""
    exe, title = adapter.foreground()
    buffer.record(exe, now, title)
