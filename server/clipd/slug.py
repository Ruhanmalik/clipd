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

# Collapses every run of non-alphanumerics to a single "-". This is what
# enforces spec §4's rules: the Windows-illegal set < > : " / \ | ? * and the
# control characters are all non-alphanumeric, so they are stripped here. A
# separate pass for them was verified to make no difference to any output.
_NON_SLUG = re.compile(r"[^a-z0-9]+")

FALLBACK = "unknown"


def slugify_game(name: str | None) -> str:
    if not name:
        return FALLBACK

    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _NON_SLUG.sub("-", text.lower())
    text = text.strip("-.")

    if not text:
        return FALLBACK
    if text.upper() in _WINDOWS_RESERVED:
        return f"{text}-game"
    return text
