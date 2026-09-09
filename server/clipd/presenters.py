"""Pure display helpers for the templates.

Kept out of both the templates and the route handlers: a Jinja template cannot
be unit tested, and three routes need the same shaping. Nothing here touches
the clock, the database, or the disk — `now` is always passed in.
"""
from __future__ import annotations

from .db import Clip
from .notify import human_bytes

# Re-exported so templates have a single import site for value formatting.
__all__ = [
    "display_title", "human_duration", "relative_time", "human_bytes",
    "pluralize", "UNKNOWN",
]

UNKNOWN = "Unknown"

MINUTE = 60
HOUR = 3600
DAY = 86400


def display_title(clip: Clip) -> str:
    """The clip's own title, else its game, else Unknown.

    Unknown matches what the watcher already sends for an unresolved game
    (spec §5), so a re-tagging-queue tile reads the same everywhere.
    """
    return clip.title or clip.game or UNKNOWN


def human_duration(seconds: float | None) -> str:
    """m:ss, or h:mm:ss past an hour. Empty for a screenshot."""
    if not seconds or seconds <= 0:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, HOUR)
    minutes, secs = divmod(remainder, MINUTE)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def pluralize(count: int, noun: str) -> str:
    """`1 capture`, `3 captures`. Templates interpolate; they do not compute."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _plural(count: int, unit: str) -> str:
    return f"{pluralize(count, unit)} ago"


def relative_time(then: int, now: int) -> str:
    """Coarse age. Clamped at zero: /ingest accepts a day of clock skew, so a
    row's created_at can genuinely sit in the future, and "-3 minutes ago"
    would be the visible result."""
    delta = max(0, now - then)

    if delta < MINUTE:
        return "just now"
    if delta < HOUR:
        return _plural(delta // MINUTE, "minute")
    if delta < DAY:
        return _plural(delta // HOUR, "hour")
    if delta < 2 * DAY:
        return "yesterday"
    return _plural(delta // DAY, "day")
