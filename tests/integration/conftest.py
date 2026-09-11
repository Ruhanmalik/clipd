"""One real clipwatch talking to one real clipd, in a single process.

Everything here is the genuine article except ffmpeg and ntfy: the client
builds the request, the server parses it, SQLite and the capture store are
real. That is the whole point — each unit suite currently tests its side
against a hand-written imitation of the other, so a contract drift between
them leaves both green.
"""
import json
import pytest
from fastapi.testclient import TestClient

from clipd.app import create_app
from clipd.config import Config as ServerConfig
from clipd.media import ProbeResult
from clipwatch.config import Config as ClientConfig
from clipwatch.jobs import Job

TOKEN = "shared-ingest-token"
BASE_URL = "http://clipd-server:8000"


@pytest.fixture
def server_cfg(tmp_path):
    return ServerConfig.from_env({
        "INGEST_TOKEN": TOKEN,
        "DATA_DIR": str(tmp_path / "store"),
        "BASE_URL": BASE_URL,
        "MAX_STORE_BYTES": "10MB",
        "SWEEP_INTERVAL_S": "0",   # no background sweep during tests
    })


@pytest.fixture
def server(server_cfg, monkeypatch):
    """The real app, with only the two ffmpeg calls and the ntfy push stubbed.

    TestClient is an httpx.Client subclass, so clipwatch.upload() accepts it
    directly and every request goes through the real ASGI stack — routing,
    auth dependency, multipart parsing, storage — with no network.
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

    with TestClient(create_app(server_cfg)) as client:
        yield client


@pytest.fixture
def client_cfg(tmp_path):
    """A real clipwatch Config, sharing the server's token and URL."""
    f = tmp_path / "config.toml"
    f.write_text(
        f'server_url = "{BASE_URL}"\n'
        f'watch_dir = "{(tmp_path / "watch").as_posix()}"\n'
        'source_host = "gaming-pc"\n'
    )
    return ClientConfig.load(f, {"CLIPD_TOKEN": TOKEN})


@pytest.fixture
def make_job(tmp_path):
    """A queued Job backed by a real file on disk."""
    def _make(capture_uuid="uuid-1", *, kind="clip", game="Counter-Strike 2",
              game_exe="cs2.exe", captured_at=1757260800, body=b"videodata",
              name=None):
        path = tmp_path / (name or f"{capture_uuid}.mp4")
        path.write_bytes(body)
        return Job(capture_uuid=capture_uuid, path=str(path), kind=kind,
                   game=game, game_exe=game_exe, captured_at=captured_at,
                   attempts=0, next_attempt_at=0.0, source_path=str(path))
    return _make


@pytest.fixture
def sent_meta(monkeypatch):
    """Record the exact meta JSON the client put on the wire.

    Captured where the server parses it, so it is what was actually
    transmitted rather than what the test thinks was transmitted.
    """
    seen = {}
    from clipd import app as app_module
    original = app_module.IngestMeta.model_validate_json

    def recording(raw, *a, **kw):
        seen["raw"] = raw
        seen["keys"] = set(json.loads(raw))
        return original(raw, *a, **kw)

    monkeypatch.setattr(app_module.IngestMeta, "model_validate_json", recording)
    return seen
