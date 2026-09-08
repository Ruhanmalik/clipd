"""The only surface the rest of clipwatch sees for OS-specific behaviour."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PlatformAdapter(Protocol):
    def foreground(self) -> tuple[str | None, str | None]:
        """Return (executable name, window title) of the foreground window."""

    def set_clipboard(self, text: str) -> bool:
        """Put text on the clipboard. False on failure — never raises."""

    def notify(self, title: str, body: str) -> bool:
        """Show a desktop notification. False on failure — never raises."""
