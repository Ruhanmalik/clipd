import json
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
        "MAX_STORE_BYTES": "1000",
        "SWEEP_INTERVAL_S": "0",
    })


@pytest.fixture
def client(cfg, monkeypatch):
    async def fake_probe(path):
        return ProbeResult(90.0, 1920, 1080)

    async def fake_thumb(src, dest, at_s):
        return False

    async def fake_notify(*a, **kw):
        return True

    monkeypatch.setattr("clipd.app.media.probe", fake_probe)
    monkeypatch.setattr("clipd.app.media.make_thumbnail", fake_thumb)
    monkeypatch.setattr("clipd.app.notify_capture", fake_notify)

    with TestClient(create_app(cfg)) as c:
        yield c


def test_healthz_reports_ok_on_empty_store(client):
    payload = client.get("/healthz").json()
    assert payload == {"status": "ok", "clips": 0, "bytes": 0,
                       "budget_bytes": 1000, "used_pct": 0.0}


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").status_code == 200


def test_healthz_counts_ingested_clips(client):
    meta = {"kind": "clip", "capture_uuid": "u-1", "game": "Halo",
            "source_host": "h", "captured_at": 1757260800}
    client.post("/ingest", headers={"Authorization": "Bearer secret-token"},
                files={"file": ("a.mp4", b"x" * 250, "video/mp4")},
                data={"meta": json.dumps(meta)})

    payload = client.get("/healthz").json()
    assert payload["clips"] == 1
    assert payload["bytes"] == 250
    assert payload["used_pct"] == 25.0
