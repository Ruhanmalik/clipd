"""GET /v/<id> — the detail page the clipboard URL points at."""
from clipd import db


def test_unknown_clip_is_404(client):
    assert client.get("/v/nope123").status_code == 404


def test_a_clip_renders_a_video_player_pointed_at_the_media_route(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))

    body = client.get("/v/aB3xY9z").text

    assert "<video" in body
    assert "/m/aB3xY9z" in body


def test_a_screenshot_renders_an_image_not_a_player(client, make_clip):
    clip = make_clip("shot123", created_at=100, kind="screenshot",
                     duration_s=None,
                     rel_path="shots/halo/2026/09/shot123.png")
    db.insert_clip(client.app.state.conn, clip)

    body = client.get("/v/shot123").text

    assert "<video" not in body
    assert "<img" in body and "/m/shot123" in body


def test_detail_page_offers_a_download(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))
    assert "/d/aB3xY9z" in client.get("/v/aB3xY9z").text


def test_detail_page_links_back_to_the_game(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))
    assert "/g/halo" in client.get("/v/aB3xY9z").text


def test_detail_page_shows_resolution_duration_and_size(client, make_clip):
    db.insert_clip(client.app.state.conn, make_clip("aB3xY9z", created_at=100))

    body = client.get("/v/aB3xY9z").text

    assert "1920×1080" in body
    assert "1:30" in body
    assert "1000 B" in body


def test_a_clip_with_no_probe_data_still_renders(client, make_clip):
    """ffprobe can fail at ingest; those columns are nullable for that reason.
    The rows should be omitted, not rendered as None."""
    clip = make_clip("sparse1", created_at=100, duration_s=None,
                     width=None, height=None)
    db.insert_clip(client.app.state.conn, clip)

    response = client.get("/v/sparse1")

    assert response.status_code == 200
    assert "None" not in response.text
    assert "Duration" not in response.text
    assert "Resolution" not in response.text


def test_detail_page_escapes_a_hostile_title(client, make_clip):
    clip = make_clip("xss1234", created_at=100,
                     title="<img src=x onerror=alert(1)>")
    db.insert_clip(client.app.state.conn, clip)

    body = client.get("/v/xss1234").text

    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img" in body
