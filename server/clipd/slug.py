"""Turn a game's display name into a path-safe directory component.

Must be valid on Linux (the server) and Windows (the watcher stages files
locally before upload), so the Windows rules are the binding ones.
"""
from __future__ import annotations

import re
import unicodedata

_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NON_SLUG = re.compile(r"[^a-z0-9]+")

FALLBACK = "unknown"


def slugify_game(name: str | None) -> str:
    if not name:
        return FALLBACK

    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _ILLEGAL.sub(" ", text)
    text = _NON_SLUG.sub("-", text.lower())
    text = text.strip("-.")

    if not text:
        return FALLBACK
    if text.upper() in _WINDOWS_RESERVED:
        return f"{text}-game"
    return text
