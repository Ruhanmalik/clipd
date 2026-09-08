"""Which game was being played over the capture window.

design §5: the window title at hotkey time is unreliable — alt-tabbing to
Discord before hitting the hotkey would file the clip under "Discord". So we
sample the foreground process executable on a timer and take the most common
non-ignored one across the replay-buffer window.

Time is injected rather than read from the clock, so the rule is testable.
"""
from __future__ import annotations

import threading
from collections import deque


class ExeRingBuffer:
    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._samples: deque[tuple[float, str]] = deque()
        self.titles: dict[str, str] = {}
        # The sampler thread writes while a capture reads; without this the
        # reader raises "deque mutated during iteration" and, because the
        # caller swallows it, the capture is silently dropped.
        self._lock = threading.Lock()

    def record(self, exe: str | None, now: float, title: str | None = None) -> None:
        if not exe:
            return
        key = exe.lower()
        with self._lock:
            self._samples.append((now, key))
            if title:
                self.titles[key] = title
            self._evict(now)
            self._trim_titles()

    def _trim_titles(self) -> None:
        """Keep titles bounded — it is a cache, not a log."""
        if len(self.titles) <= 32:
            return
        live = {exe for _, exe in self._samples}
        for exe in [k for k in self.titles if k not in live]:
            del self.titles[exe]

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def dominant(self, ignore: frozenset[str], now: float) -> str | None:
        with self._lock:
            self._evict(now)
            samples = list(self._samples)   # snapshot; iterate outside the lock

        counts: dict[str, int] = {}
        last_seen: dict[str, float] = {}
        for at, exe in samples:
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
