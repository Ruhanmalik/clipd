import sys
import pytest
from clipwatch.platform import NullAdapter, get_adapter
from clipwatch.platform.base import PlatformAdapter


def test_null_adapter_satisfies_the_protocol():
    assert isinstance(NullAdapter(), PlatformAdapter)


def test_null_foreground_returns_nothing():
    assert NullAdapter().foreground() == (None, None)


def test_null_clipboard_and_notify_report_failure_without_raising():
    adapter = NullAdapter()
    assert adapter.set_clipboard("x") is False
    assert adapter.notify("t", "b") is False


def test_null_adapter_records_calls_for_assertions():
    adapter = NullAdapter()
    adapter.set_clipboard("http://clipd-server:8000/v/abc")
    adapter.notify("Counter-Strike 2", "90s")
    assert adapter.clipboard == "http://clipd-server:8000/v/abc"
    assert adapter.notifications == [("Counter-Strike 2", "90s")]


def test_get_adapter_returns_null_when_asked():
    assert isinstance(get_adapter("null"), NullAdapter)


def test_get_adapter_falls_back_to_null_off_windows():
    # The whole point: importing this package on macOS must never touch pywin32.
    if sys.platform != "win32":
        assert isinstance(get_adapter(), NullAdapter)


def test_get_adapter_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        get_adapter("beos")


def test_importing_the_package_does_not_import_pywin32():
    assert "win32gui" not in sys.modules
