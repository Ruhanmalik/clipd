import pytest
from clipd import db, retention
from clipd.config import Config
from clipd.retention import RetentionState, sweep, sweep_and_notify


def make_clip(clip_id, created_at, size, *, pinned=0, public_slug=None):
    return db.Clip(
        id=clip_id, public_slug=public_slug, capture_uuid=f"u-{clip_id}",
        kind="clip", title=None, filename=f"{clip_id}.mp4",
        rel_path=f"clips/halo/2026/09/{clip_id}.mp4", bytes=size,
        duration_s=90.0, width=1920, height=1080, game="Halo",
        game_slug="halo", game_exe="halo.exe", pinned=pinned,
        source_host="h", created_at=created_at,
        thumb_path=f"thumbs/{clip_id}.jpg",
    )


@pytest.fixture
def store(tmp_path):
    cfg = Config.from_env({
        "INGEST_TOKEN": "t", "DATA_DIR": str(tmp_path), "MAX_STORE_BYTES": "1000",
    })
    conn = db.connect(tmp_path / "clipd.db")
    db.init_schema(conn)
    yield conn, cfg
    conn.close()


def add(conn, cfg, clip):
    db.insert_clip(conn, clip)
    target = cfg.data_dir / clip.rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * clip.bytes)


def test_sweep_does_nothing_under_budget(store):
    conn, cfg = store
    add(conn, cfg, make_clip("a", 100, 400))

    result = sweep(conn, cfg)

    assert result.deleted == 0
    assert result.over_budget is False
    assert db.get_by_id(conn, "a") is not None


def test_sweep_deletes_oldest_first_until_under_budget(store):
    conn, cfg = store
    add(conn, cfg, make_clip("oldest", 100, 500))
    add(conn, cfg, make_clip("middle", 200, 500))
    add(conn, cfg, make_clip("newest", 300, 500))

    result = sweep(conn, cfg)  # 1500 bytes against a 1000 budget

    assert result.deleted == 1
    assert result.freed_bytes == 500
    assert db.get_by_id(conn, "oldest") is None
    assert db.get_by_id(conn, "middle") is not None
    assert db.get_by_id(conn, "newest") is not None


def test_sweep_removes_the_file_and_thumbnail(store):
    conn, cfg = store
    add(conn, cfg, make_clip("a", 100, 900))
    add(conn, cfg, make_clip("b", 200, 900))
    thumb = cfg.data_dir / "thumbs" / "a.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpeg")

    sweep(conn, cfg)

    assert not (cfg.data_dir / "clips" / "halo" / "2026" / "09" / "a.mp4").exists()
    assert not thumb.exists()


def test_sweep_never_deletes_pinned_or_shared(store):
    conn, cfg = store
    add(conn, cfg, make_clip("pinned", 100, 800, pinned=1))
    add(conn, cfg, make_clip("shared", 200, 800, public_slug="slugslugslugslugslug12"))

    result = sweep(conn, cfg)  # 1600 bytes against a 1000 budget

    assert result.deleted == 0
    assert result.over_budget is True
    assert result.unprunable_bytes == 1600
    assert db.get_by_id(conn, "pinned") is not None
    assert db.get_by_id(conn, "shared") is not None


def test_sweep_prunes_around_pinned_clips_to_get_under_budget(store):
    conn, cfg = store
    add(conn, cfg, make_clip("prunable", 100, 300))
    add(conn, cfg, make_clip("pinned", 200, 900, pinned=1))

    result = sweep(conn, cfg)  # 1200 bytes against a 1000 budget

    assert result.deleted == 1
    assert result.total_bytes == 900
    assert result.over_budget is False
    assert db.get_by_id(conn, "pinned") is not None


def test_sweep_stays_over_budget_when_protected_clips_exceed_it(store):
    conn, cfg = store
    add(conn, cfg, make_clip("prunable", 100, 200))
    add(conn, cfg, make_clip("pinned", 200, 1500, pinned=1))

    result = sweep(conn, cfg)  # 1700 bytes against a 1000 budget

    assert result.deleted == 1          # took everything it was allowed to
    assert result.total_bytes == 1500
    assert result.over_budget is True   # and is still over — this is the alert case


async def test_sweep_and_notify_warns_once_at_eighty_percent(store, monkeypatch):
    conn, cfg = store
    sent = []

    async def fake_notify(config, *, title, body, priority="default"):
        sent.append((title, priority))
        return True

    monkeypatch.setattr("clipd.retention.notify_retention", fake_notify)
    add(conn, cfg, make_clip("a", 100, 850))

    state = RetentionState()
    await sweep_and_notify(conn, cfg, state)
    await sweep_and_notify(conn, cfg, state)  # still high; must not re-warn

    assert len(sent) == 1
    assert "80%" in sent[0][0] or "budget" in sent[0][0].lower()


async def test_sweep_and_notify_sends_high_priority_alert_when_unprunable(store, monkeypatch):
    conn, cfg = store
    sent = []

    async def fake_notify(config, *, title, body, priority="default"):
        sent.append((title, body, priority))
        return True

    monkeypatch.setattr("clipd.retention.notify_retention", fake_notify)
    add(conn, cfg, make_clip("pinned", 100, 1500, pinned=1))

    await sweep_and_notify(conn, cfg, RetentionState())

    assert any(p == "high" for _, _, p in sent)
    assert any("unprunable" in b.lower() or "pinned" in b.lower() for _, b, _ in sent)
