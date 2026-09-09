"""GET / — the game-first landing page."""
from clipd import db



def test_empty_store_renders_an_empty_state(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Nothing captured yet" in response.text


def test_landing_lists_a_tile_per_game(client, make_clip):
    conn = client.app.state.conn
    db.insert_clip(conn, make_clip("a", created_at=100))
    db.insert_clip(conn, make_clip("b", created_at=200))
    db.insert_clip(conn, make_clip("c", created_at=300, game="Doom",
                                   game_slug="doom"))

    body = client.get("/").text

    assert "/g/halo" in body
    assert "/g/doom" in body
    assert "Halo" in body and "Doom" in body


def test_tile_shows_the_capture_count(client, make_clip):
    conn = client.app.state.conn
    for i in range(3):
        db.insert_clip(conn, make_clip(f"c{i}", created_at=i))

    assert "3 captures" in client.get("/").text


def test_tile_shows_a_singular_count_for_one_capture(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("only", created_at=1))
    body = client.get("/").text

    assert "1 capture" in body
    assert "1 captures" not in body


def test_recent_strip_links_each_capture_to_its_detail_page(client, make_clip):
    conn = client.app.state.conn
    db.insert_clip(conn, make_clip("recent1", created_at=500))

    assert "/v/recent1" in client.get("/").text


def test_recent_strip_is_capped(client, make_clip):
    """The strip is a glance, not a listing — /g/<slug> is the listing."""
    conn = client.app.state.conn
    for i in range(20):
        db.insert_clip(conn, make_clip(f"c{i:02d}", created_at=i))

    body = client.get("/").text
    linked = sum(1 for i in range(20) if f"/v/c{i:02d}" in body)
    assert linked == 8


def test_a_game_with_no_thumbnail_renders_no_broken_image(client, make_clip):
    """thumb_path is NULL whenever ffmpeg failed at ingest. The tile should
    fall back to the empty frame, not to a broken-image glyph."""
    db.insert_clip(client.app.state.conn,
                   make_clip("nothumb", created_at=1, thumb=False))
    response = client.get("/")
    assert response.status_code == 200
    assert "/t/nothumb" not in response.text


def test_html_escapes_a_hostile_game_name(client, make_clip):
    """game comes from a window title on the client (spec §5) — untrusted."""
    db.insert_clip(client.app.state.conn, make_clip(
        "xss1234", created_at=1, game="<script>alert(1)</script>",
        game_slug="script-alert-1-script"))

    body = client.get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
