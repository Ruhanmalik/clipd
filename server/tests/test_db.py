import sqlite3

import pytest
from clipd.db import (
    Clip, connect, init_schema, insert_clip, get_by_id, get_by_capture_uuid,
    store_totals, prunable_oldest_first, unprunable_bytes, delete_clip,
)


def make_clip(**overrides) -> Clip:
    base = dict(
        id="aB3xY9z", public_slug=None, capture_uuid="uuid-1", kind="clip",
        title=None, filename="aB3xY9z.mp4",
        rel_path="clips/counter-strike-2/2026/09/aB3xY9z.mp4",
        bytes=1000, duration_s=90.0, width=1920, height=1080,
        game="Counter-Strike 2", game_slug="counter-strike-2", game_exe="cs2.exe",
        pinned=0, source_host="gaming-pc", created_at=1757260800,
        thumb_path="thumbs/aB3xY9z.jpg",
    )
    base.update(overrides)
    return Clip(**base)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


def test_insert_and_get_roundtrip(conn):
    clip = make_clip()
    insert_clip(conn, clip)
    assert get_by_id(conn, "aB3xY9z") == clip


def test_get_by_id_returns_none_when_absent(conn):
    assert get_by_id(conn, "nope123") is None


def test_get_by_capture_uuid_finds_the_row(conn):
    insert_clip(conn, make_clip())
    found = get_by_capture_uuid(conn, "uuid-1")
    assert found is not None and found.id == "aB3xY9z"


def test_capture_uuid_is_unique(conn):
    insert_clip(conn, make_clip())
    with pytest.raises(sqlite3.IntegrityError):
        insert_clip(conn, make_clip(id="different"))


def test_init_schema_is_idempotent(conn, tmp_path):
    init_schema(conn)  # second call must not raise
    insert_clip(conn, make_clip())
    assert store_totals(conn) == (1, 1000)


def test_store_totals_sums_count_and_bytes(conn):
    insert_clip(conn, make_clip(id="a", capture_uuid="u1", bytes=100))
    insert_clip(conn, make_clip(id="b", capture_uuid="u2", bytes=250))
    assert store_totals(conn) == (2, 350)


def test_store_totals_on_empty_store(conn):
    assert store_totals(conn) == (0, 0)


def test_prunable_excludes_pinned_and_shared(conn):
    insert_clip(conn, make_clip(id="old", capture_uuid="u1", created_at=100))
    insert_clip(conn, make_clip(id="pin", capture_uuid="u2", created_at=200, pinned=1))
    insert_clip(conn, make_clip(id="shr", capture_uuid="u3", created_at=300,
                                public_slug="sharedslug1234567890ab"))
    insert_clip(conn, make_clip(id="new", capture_uuid="u4", created_at=400))

    ids = [c.id for c in prunable_oldest_first(conn)]
    assert ids == ["old", "new"]  # oldest first, protected rows omitted


def test_unprunable_bytes_counts_only_protected_rows(conn):
    insert_clip(conn, make_clip(id="a", capture_uuid="u1", bytes=100))
    insert_clip(conn, make_clip(id="b", capture_uuid="u2", bytes=200, pinned=1))
    insert_clip(conn, make_clip(id="c", capture_uuid="u3", bytes=400,
                                public_slug="slugslugslugslugslug12"))
    assert unprunable_bytes(conn) == 600


def test_delete_clip_removes_the_row(conn):
    insert_clip(conn, make_clip())
    delete_clip(conn, "aB3xY9z")
    assert get_by_id(conn, "aB3xY9z") is None
