"""Read-side queries behind the gallery. Step 3a, spec §8/§8a."""
import pytest
from clipd import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()


def add(conn, *clips):
    for clip in clips:
        db.insert_clip(conn, clip)


def test_list_games_groups_and_counts(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100),
        make_clip("b", created_at=200),
        make_clip("c", created_at=150, game="Counter-Strike 2",
                  game_slug="counter-strike-2"))

    games = {g.game_slug: g for g in db.list_games(conn)}

    assert games["halo"].clips == 2
    assert games["halo"].bytes == 2000
    assert games["halo"].latest_at == 200
    assert games["counter-strike-2"].clips == 1


def test_list_games_orders_most_recent_first(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100, game="Halo", game_slug="halo"),
        make_clip("b", created_at=300, game="Doom", game_slug="doom"),
        make_clip("c", created_at=200, game="Myst", game_slug="myst"))

    assert [g.game_slug for g in db.list_games(conn)] == ["doom", "myst", "halo"]


def test_list_games_covers_with_the_newest_clip(conn, make_clip):
    add(conn,
        make_clip("old", created_at=100),
        make_clip("new", created_at=900))

    (halo,) = db.list_games(conn)
    assert halo.cover_id == "new"


def test_list_games_reports_whether_the_cover_has_a_thumbnail(conn, make_clip):
    add(conn, make_clip("nothumb", created_at=100, thumb=False))
    (game,) = db.list_games(conn)
    assert game.cover_id == "nothumb"
    assert game.cover_thumb is None


def test_list_games_is_empty_on_an_empty_store(conn):
    assert db.list_games(conn) == []


def test_recent_clips_returns_newest_first_and_respects_limit(conn, make_clip):
    add(conn, *[make_clip(f"c{i}", created_at=i) for i in range(10)])

    recent = db.recent_clips(conn, limit=3)

    assert [c.id for c in recent] == ["c9", "c8", "c7"]


def test_list_by_game_filters_to_one_game(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100),
        make_clip("b", created_at=200, game="Doom", game_slug="doom"))

    assert [c.id for c in db.list_by_game(conn, "halo")] == ["a"]


def test_list_by_game_filters_by_kind(conn, make_clip):
    add(conn,
        make_clip("clip1", created_at=100),
        make_clip("shot1", created_at=200, kind="screenshot"))

    assert [c.id for c in db.list_by_game(conn, "halo", kind="screenshot")] == ["shot1"]
    assert [c.id for c in db.list_by_game(conn, "halo", kind="clip")] == ["clip1"]


def test_list_by_game_paginates_by_keyset(conn, make_clip):
    add(conn, *[make_clip(f"c{i}", created_at=i) for i in range(5)])

    first = db.list_by_game(conn, "halo", limit=2)
    assert [c.id for c in first] == ["c4", "c3"]

    cursor = (first[-1].created_at, first[-1].id)
    second = db.list_by_game(conn, "halo", limit=2, before=cursor)
    assert [c.id for c in second] == ["c2", "c1"]


def test_pagination_advances_when_two_captures_share_a_second(conn, make_clip):
    """A whole page at one timestamp must not stall the cursor.

    Comparing created_at alone would return the same rows forever, because
    every row on the next page also satisfies created_at <= the cursor.
    """
    add(conn, *[make_clip(f"c{i}", created_at=500) for i in range(4)])

    first = db.list_by_game(conn, "halo", limit=2)
    cursor = (first[-1].created_at, first[-1].id)
    second = db.list_by_game(conn, "halo", limit=2, before=cursor)

    assert {c.id for c in first} & {c.id for c in second} == set()
    assert len(second) == 2


def test_count_by_game_counts_with_and_without_a_kind(conn, make_clip):
    add(conn,
        make_clip("a", created_at=100),
        make_clip("b", created_at=200),
        make_clip("s", created_at=300, kind="screenshot"))

    assert db.count_by_game(conn, "halo") == 3
    assert db.count_by_game(conn, "halo", kind="clip") == 2
    assert db.count_by_game(conn, "doom") == 0
