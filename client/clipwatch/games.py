"""Map a process executable to a pretty game name.

Ships locally with the watcher: detection is entirely client-side and must
keep working while clipd is unreachable, which is exactly when the retry
queue matters.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

UNKNOWN = "Unknown"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


def load_games(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {str(k).lower(): str(v) for k, v in raw.items()}


def _clean_title(title: str | None) -> str:
    if not title:
        return ""
    return _WHITESPACE.sub(" ", _CONTROL.sub(" ", title)).strip()


def resolve_game(
    exe: str | None, mapping: dict[str, str], window_title: str | None = None
) -> str:
    if exe:
        mapped = mapping.get(exe.lower())
        if mapped:
            return mapped

    title = _clean_title(window_title)
    if title:
        return title

    # design §5 stops here deliberately: Unknown is a normal gallery tile and
    # doubles as the re-tagging queue. Falling back to the exe stem would give
    # every unmapped game its own tile and scatter that queue. The exe is not
    # lost either way — it is sent separately as game_exe.
    return UNKNOWN
