"""GET /g/<game-slug> — one game's captures."""
import re

import pytest
from clipd import db, web


def seed(client, make_clip, count, **kw):
    conn = client.app.state.conn
    for i in range(count):
        db.insert_clip(conn, make_clip(f"c{i:03d}", created_at=1000 + i, **kw))


def test_unknown_game_is_404(client):
    assert client.get("/g/never-played").status_code == 404


def test_game_page_lists_its_captures(client, make_clip):
    seed(client, make_clip, 3)

    body = client.get("/g/halo").text

    assert "/v/c000" in body and "/v/c002" in body
    assert "Halo" in body


def test_game_page_shows_the_total_count(client, make_clip):
    seed(client, make_clip, 5)
    assert "5 captures" in client.get("/g/halo").text


def test_kind_filter_narrows_the_listing(client, make_clip):
    conn = client.app.state.conn
    db.insert_clip(conn, make_clip("aclip", created_at=100))
    db.insert_clip(conn, make_clip("ashot", created_at=200, kind="screenshot"))

    clips_only = client.get("/g/halo?kind=clip").text
    assert "/v/aclip" in clips_only and "/v/ashot" not in clips_only

    shots_only = client.get("/g/halo?kind=screenshot").text
    assert "/v/ashot" in shots_only and "/v/aclip" not in shots_only


def test_an_unrecognised_kind_is_ignored_rather_than_erroring(client, make_clip):
    """A hand-edited URL should show everything, not a stack trace."""
    seed(client, make_clip, 2)
    response = client.get("/g/halo?kind=nonsense")

    assert response.status_code == 200
    assert "/v/c000" in response.text


def test_a_full_first_page_offers_a_next_link(client, make_clip):
    seed(client, make_clip, web.PAGE_SIZE + 1)

    body = client.get("/g/halo").text

    assert "before=" in body


def test_a_short_page_offers_no_next_link(client, make_clip):
    seed(client, make_clip, 3)
    response = client.get("/g/halo")
    assert response.status_code == 200
    assert "before=" not in response.text


def test_the_next_link_returns_the_following_page(client, make_clip):
    seed(client, make_clip, web.PAGE_SIZE + 3)

    first = client.get("/g/halo")
    # The oldest row on page one is the cursor; page two starts below it.
    oldest = db.list_by_game(client.app.state.conn, "halo",
                             limit=web.PAGE_SIZE)[-1]
    second = client.get(f"/g/halo?before={web.encode_cursor(oldest)}")

    assert second.status_code == 200
    assert f"/v/{oldest.id}" not in second.text
    assert "/v/c000" in second.text


def test_the_kind_filter_survives_pagination(client, make_clip):
    """The Older link must carry the filter, or page two silently widens it."""
    seed(client, make_clip, web.PAGE_SIZE + 1)
    body = client.get("/g/halo?kind=clip").text

    assert re.search(r'href="/g/halo\?before=[^"]*&amp;kind=clip"', body)


def test_a_malformed_cursor_serves_the_first_page(client, make_clip):
    """A truncated or hand-edited URL should not 500."""
    seed(client, make_clip, 3)

    for bad in ("", "garbage", "123", "notanumber_abc", "1_2_3"):
        response = client.get(f"/g/halo?before={bad}")
        assert response.status_code == 200, bad
        assert "/v/c002" in response.text


def test_cursor_roundtrips(make_clip):
    clip = make_clip("aB3xY9z", created_at=1757260800)
    assert web.encode_cursor(clip) == "1757260800_aB3xY9z"
    assert web.decode_cursor("1757260800_aB3xY9z") == (1757260800, "aB3xY9z")


def test_decode_cursor_rejects_junk():
    for bad in (None, "", "x", "a_b", "1_", "_1"):
        assert web.decode_cursor(bad) is None
