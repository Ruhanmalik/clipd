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
        "BASE_URL": "http://clipd-server:8000",
        "SWEEP_INTERVAL_S": "0",   # no background sweep during tests
    })


@pytest.fixture
def client(cfg, monkeypatch):
    async def fake_probe(path):
        return ProbeResult(duration_s=90.0, width=1920, height=1080)

    async def fake_thumb(src, dest, at_s):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr("clipd.app.media.probe", fake_probe)
    monkeypatch.setattr("clipd.app.media.make_thumbnail", fake_thumb)
    monkeypatch.setattr("clipd.app.notify_capture", lambda *a, **kw: _true())

    with TestClient(create_app(cfg)) as c:
        yield c


async def _true():
    return True


def post_clip(client, *, uuid="u-1", game="Counter-Strike 2", token="secret-token",
              kind="clip", filename="replay.mp4", body=b"videodata"):
    meta = {
        "kind": kind, "capture_uuid": uuid, "game": game,
        "game_exe": "cs2.exe", "source_host": "gaming-pc",
        "captured_at": 1757260800,
    }
    return client.post(
        "/ingest",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, body, "video/mp4")},
        data={"meta": json.dumps(meta)},
    )


def test_ingest_stores_file_under_game_and_date(client, cfg):
    response = post_clip(client)
    assert response.status_code == 200

    clip_id = response.json()["id"]
    stored = cfg.data_dir / "clips" / "counter-strike-2" / "2025" / "09" / f"{clip_id}.mp4"
    assert stored.read_bytes() == b"videodata"


def test_ingest_returns_id_and_url(client):
    payload = post_clip(client).json()
    assert len(payload["id"]) == 7
    assert payload["url"] == f"http://clipd-server:8000/v/{payload['id']}"


def test_ingest_writes_thumbnail(client, cfg):
    clip_id = post_clip(client).json()["id"]
    assert (cfg.data_dir / "thumbs" / f"{clip_id}.jpg").exists()


def test_ingest_records_probed_metadata(client, cfg):
    from clipd import db
    clip_id = post_clip(client).json()["id"]
    conn = db.connect(cfg.data_dir / "clipd.db")
    clip = db.get_by_id(conn, clip_id)
    assert (clip.duration_s, clip.width, clip.height) == (90.0, 1920, 1080)
    assert clip.game == "Counter-Strike 2"
    assert clip.game_slug == "counter-strike-2"
    assert clip.game_exe == "cs2.exe"
    assert clip.bytes == len(b"videodata")


def test_repeated_capture_uuid_returns_same_id_without_second_file(client, cfg):
    first = post_clip(client, uuid="same").json()
    second = post_clip(client, uuid="same").json()

    assert first["id"] == second["id"]
    assert second["duplicate"] is True
    stored = list((cfg.data_dir / "clips").rglob("*.mp4"))
    assert len(stored) == 1


def test_missing_game_files_under_unknown(client, cfg):
    clip_id = post_clip(client, uuid="u-2", game=None).json()["id"]
    assert (cfg.data_dir / "clips" / "unknown" / "2025" / "09" / f"{clip_id}.mp4").exists()


def test_screenshot_goes_to_shots_root(client, cfg):
    clip_id = post_clip(
        client, uuid="u-3", kind="screenshot", filename="shot.png", body=b"pngdata"
    ).json()["id"]
    assert (cfg.data_dir / "shots" / "counter-strike-2" / "2025" / "09" / f"{clip_id}.png").exists()


def test_rejects_missing_token(client):
    response = client.post("/ingest", files={"file": ("a.mp4", b"x", "video/mp4")},
                           data={"meta": "{}"})
    assert response.status_code == 401


def test_rejects_wrong_token(client):
    assert post_clip(client, token="wrong-token").status_code == 401


def test_rejects_unknown_kind(client):
    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret-token"},
        files={"file": ("a.mp4", b"x", "video/mp4")},
        data={"meta": json.dumps({"kind": "video", "capture_uuid": "u",
                                  "source_host": "h"})},
    )
    assert response.status_code == 422
