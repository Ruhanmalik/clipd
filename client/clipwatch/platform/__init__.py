"""Adapter selection. windows.py is imported lazily so macOS never sees pywin32."""
from __future__ import annotations

import sys

from .base import PlatformAdapter
from .null import NullAdapter

__all__ = ["PlatformAdapter", "NullAdapter", "get_adapter"]


def get_adapter(name: str | None = None) -> PlatformAdapter:
    if name is None:
        name = "windows" if sys.platform == "win32" else "null"

    if name == "null":
        return NullAdapter()
    if name == "windows":
        from .windows import WindowsAdapter

        return WindowsAdapter()
    raise ValueError(f"unknown platform adapter: {name!r}")
