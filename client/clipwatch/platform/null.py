"""No-op adapter: development on macOS, and assertions in tests."""
from __future__ import annotations


class NullAdapter:
    def __init__(self) -> None:
        self.clipboard: str | None = None
        self.notifications: list[tuple[str, str]] = []

    def foreground(self) -> tuple[str | None, str | None]:
        return (None, None)

    def set_clipboard(self, text: str) -> bool:
        self.clipboard = text
        return False  # nothing was really put on a clipboard

    def notify(self, title: str, body: str) -> bool:
        self.notifications.append((title, body))
        return False
