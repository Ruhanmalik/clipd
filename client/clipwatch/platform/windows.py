"""Windows implementation. The ONLY module permitted to import win32*.

Every method swallows its errors: a failed clipboard write or toast must
never take down the daemon or fail an upload that already succeeded.
"""
from __future__ import annotations

import logging

import psutil
import win32con
import win32gui
import win32process

log = logging.getLogger(__name__)


class WindowsAdapter:
    def foreground(self) -> tuple[str | None, str | None]:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return (None, None)
            title = win32gui.GetWindowText(hwnd) or None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return (None, title)
            return (psutil.Process(pid).name(), title)
        except Exception:
            log.debug("foreground sample failed", exc_info=True)
            return (None, None)

    def set_clipboard(self, text: str) -> bool:
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            log.warning("clipboard write failed", exc_info=True)
            return False

    def notify(self, title: str, body: str) -> bool:
        try:
            from windows_toasts import Toast, WindowsToaster

            toaster = WindowsToaster("clipd")
            toast = Toast()
            toast.text_fields = [title, body]
            toaster.show_toast(toast)
            return True
        except Exception:
            # windows-toasts is optional; the clipboard is the real signal.
            log.debug("toast failed", exc_info=True)
            return False
