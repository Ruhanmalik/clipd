"""Regression tests for defects found in code review of the Step 1 server.

Each test names the failure it locks out. These are the review findings that
were independently reproduced before being fixed.
"""
import asyncio
import json

import pytest

from clipd import db
from clipd.config import Config
from clipd.storage import move_capture, remove_capture


def meta_json(**over):
    base = {"kind": "clip", "capture_uuid": "u-1", "game": "Halo",
            "source_host": "h", "captured_at": 1757260800}
    base.update(over)
    return json.dumps(base)


def post(client, *, token="secret-token", filename="a.mp4", body=b"videodata", **over):
    return client.post(
        "/ingest",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, body, "video/mp4")},
        data={"meta": meta_json(**over)},
    )


# 1 — an ffmpeg fault must not strand a full-size file the retention sweep
# cannot see. The watcher retries forever, so one orphan becomes many.
def test_failed_probe_leaves_no_orphan_file(client, cfg, monkeypatch):
    async def exploding_probe(path):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr("clipd.app.media.probe", exploding_probe)
    post(client)

    assert list(cfg.data_dir.rglob("*.mp4")) == []


def test_failed_thumbnail_leaves_no_orphan_file(client, cfg, monkeypatch):
    async def exploding_thumb(src, dest, at_s):
        raise OSError("cannot fork")

    monkeypatch.setattr("clipd.app.media.make_thumbnail", exploding_thumb)
    post(client)

    assert list(cfg.data_dir.rglob("*.mp4")) == []


# make_thumbnail documents "never raises" — that must hold when the binary
# is missing entirely, not only when it exits non-zero.
async def test_make_thumbnail_survives_missing_ffmpeg_binary(monkeypatch, tmp_path):
    from clipd.media import make_thumbnail

    async def no_binary(*a, **kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr("asyncio.create_subprocess_exec", no_binary)
    assert await make_thumbnail(tmp_path / "a.mp4", tmp_path / "t.jpg", 1.0) is False


async def test_probe_survives_missing_ffprobe_binary(monkeypatch, tmp_path):
    from clipd.media import probe, EMPTY

    async def no_binary(*a, **kw):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr("asyncio.create_subprocess_exec", no_binary)
    assert await probe(tmp_path / "a.mp4") == EMPTY


# 3 — a non-ASCII Authorization header reached hmac.compare_digest and
# returned 500. Unauthenticated input must never crash the handler.
def test_non_ascii_authorization_is_rejected_not_crashed(client):
    response = client.post(
        "/ingest",
        headers={b"Authorization": "Bearer sécret".encode("latin-1")},
        files={"file": ("a.mp4", b"x", "video/mp4")},
        data={"meta": meta_json()},
    )
    assert response.status_code == 401


# 5 — the loser of an idempotency race removed its capture but left its thumb.
def test_idempotency_race_loser_leaves_no_thumbnail(client, cfg, monkeypatch):
    winner = post(client, capture_uuid="race").json()["id"]

    real_lookup = db.get_by_capture_uuid
    calls = {"n": 0}

    def lookup_pretending_row_is_absent(conn, capture_uuid):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # simulate the pre-check losing the race
        return real_lookup(conn, capture_uuid)

    monkeypatch.setattr("clipd.app.db.get_by_capture_uuid",
                        lookup_pretending_row_is_absent)

    payload = post(client, capture_uuid="race").json()
    assert payload["id"] == winner and payload["duplicate"] is True

    thumbs = {p.stem for p in (cfg.data_dir / "thumbs").glob("*.jpg")}
    assert thumbs == {winner}, f"orphaned thumbnail(s): {thumbs - {winner}}"


# 6 — an unsynced client clock produced a 500 and nonsense date shards.
@pytest.mark.parametrize("bogus", [99999999999999, -99999999999, 0])
def test_absurd_captured_at_is_rejected(client, bogus):
    assert post(client, captured_at=bogus).status_code == 422


def test_plausible_captured_at_is_accepted(client):
    assert post(client, captured_at=1757260800).status_code == 200


# 9 — the stored extension came straight from the client filename.
@pytest.mark.parametrize("filename", ["evil.html", "run.sh", "x" + "y" * 300 + ".mp4"])
def test_unexpected_extension_falls_back_to_default(client, cfg, filename):
    clip_id = post(client, filename=filename, capture_uuid=filename).json()["id"]
    assert (cfg.data_dir / "clips" / "halo" / "2025" / "09" / f"{clip_id}.mp4").exists()


def test_known_extension_is_preserved(client, cfg):
    clip_id = post(client, filename="replay.mkv", capture_uuid="mkv").json()["id"]
    assert (cfg.data_dir / "clips" / "halo" / "2025" / "09" / f"{clip_id}.mkv").exists()


# 2 — an unauthenticated caller could spool an unbounded body to disk.
def test_oversized_upload_is_rejected(client, cfg):
    huge = str(cfg.max_upload_bytes + 1)
    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret-token", "Content-Length": huge},
        content=b"x" * 10,
    )
    assert response.status_code == 413


# 16 — the interactive docs advertised the ingest contract unauthenticated.
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_exposed(client, path):
    assert client.get(path).status_code == 404


# 13 — compose passes BASE_URL through as "", which is not a missing key, so
# the default never applied and /ingest returned a bare path as the URL.
def test_empty_base_url_falls_back_to_default():
    assert Config.from_env({"INGEST_TOKEN": "t", "BASE_URL": ""}).base_url \
        == "http://localhost:8000"


# 14 — a blanked MAX_STORE_BYTES meant "budget 0", i.e. delete everything.
@pytest.mark.parametrize("bad", ["0", "0GB"])
def test_zero_store_budget_is_fatal(bad):
    with pytest.raises(ValueError, match="MAX_STORE_BYTES"):
        Config.from_env({"INGEST_TOKEN": "t", "MAX_STORE_BYTES": bad})


def test_empty_max_store_bytes_falls_back_to_default():
    cfg = Config.from_env({"INGEST_TOKEN": "t", "MAX_STORE_BYTES": ""})
    assert cfg.max_store_bytes == 50 * 1024**3


# 4 — remove_capture/move_capture unlinked whatever path they were handed.
# Not reachable from /ingest today; reachable the moment Step 3 lands
# PATCH/DELETE, which pass DB-stored rel_paths.
@pytest.mark.parametrize("escape", ["../victim.txt", "/etc/victim", "a/../../victim.txt"])
def test_remove_capture_refuses_paths_outside_data_dir(tmp_path, escape):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"important")

    with pytest.raises(ValueError):
        remove_capture(data_dir, escape)
    assert victim.exists()


def test_move_capture_refuses_paths_outside_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "clips").mkdir(parents=True)
    (data_dir / "clips" / "a.mp4").write_bytes(b"x")

    with pytest.raises(ValueError):
        move_capture(data_dir, "clips/a.mp4", "../escaped.mp4")


# 12 — the loop slept 15 minutes before its first sweep, so a service that
# crash-looped faster than the interval never swept at all.
async def test_sweep_loop_sweeps_before_sleeping(cfg, monkeypatch):
    from clipd import retention

    swept = asyncio.Event()

    async def record(conn, config, state):
        swept.set()
        return None

    monkeypatch.setattr("clipd.retention.sweep_and_notify", record)

    interval_cfg = Config.from_env({
        "INGEST_TOKEN": "t", "DATA_DIR": str(cfg.data_dir),
        "SWEEP_INTERVAL_S": "3600",
    })
    task = asyncio.create_task(
        retention.sweep_loop(None, interval_cfg, retention.RetentionState())
    )
    await asyncio.wait_for(swept.wait(), timeout=2.0)
    task.cancel()
