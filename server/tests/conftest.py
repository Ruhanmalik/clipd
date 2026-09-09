"""Shared fixtures.

test_ingest, test_healthz and test_hardening all need the same thing: an app
whose ffmpeg calls and ntfy pushes are stubbed, over a temp data dir.
"""
import pytest
from fastapi.testclient import TestClient

from clipd.app import create_app
from clipd.config import Config
from clipd.media import ProbeResult
from clipd import db


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env({
        "INGEST_TOKEN": "secret-token",
        "DATA_DIR": str(tmp_path),
        "BASE_URL": "http://midget:8000",
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


@pytest.fixture
def make_clip():
    """Build a db.Clip row. Any column can be overridden by keyword."""
    def _make(clip_id="aB3xY9z", *, created_at=1757260800, game="Halo",
              game_slug="halo", kind="clip", size=1000, thumb=True, **overrides):
        row = dict(
            id=clip_id, public_slug=None, capture_uuid=f"u-{clip_id}", kind=kind,
            title=None, filename=f"{clip_id}.mp4",
            rel_path=f"clips/{game_slug}/2026/09/{clip_id}.mp4", bytes=size,
            duration_s=90.0, width=1920, height=1080, game=game,
            game_slug=game_slug, game_exe="halo.exe", pinned=0,
            source_host="desktop-amtr56i", created_at=created_at,
            thumb_path=f"thumbs/{clip_id}.jpg" if thumb else None,
        )
        row.update(overrides)
        return db.Clip(**row)
    return _make
