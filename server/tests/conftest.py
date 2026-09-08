"""Shared fixtures.

test_ingest, test_healthz and test_hardening all need the same thing: an app
whose ffmpeg calls and ntfy pushes are stubbed, over a temp data dir.
"""
import pytest
from fastapi.testclient import TestClient

from clipd.app import create_app
from clipd.config import Config
from clipd.media import ProbeResult


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env({
        "INGEST_TOKEN": "secret-token",
        "DATA_DIR": str(tmp_path),
        "BASE_URL": "http://clipd-server:8000",
        "MAX_STORE_BYTES": "1000",
        "SWEEP_INTERVAL_S": "0",   # no background sweep during tests
    })


@pytest.fixture
def client(cfg, monkeypatch):
    """A TestClient with the two ffmpeg calls and the ntfy push stubbed out.

    raise_server_exceptions=False so tests that force a fault can assert on the
    500 and on what was left behind on disk.
    """
    async def fake_probe(path):
        return ProbeResult(duration_s=90.0, width=1920, height=1080)

    async def fake_thumb(src, dest, at_s):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return True

    async def fake_notify(*a, **kw):
        return True

    monkeypatch.setattr("clipd.app.media.probe", fake_probe)
    monkeypatch.setattr("clipd.app.media.make_thumbnail", fake_thumb)
    monkeypatch.setattr("clipd.app.notify_capture", fake_notify)

    with TestClient(create_app(cfg), raise_server_exceptions=False) as c:
        yield c
