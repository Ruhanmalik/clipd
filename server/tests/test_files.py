"""The three binary routes: inline media, thumbnail, download."""
import pytest
from clipd import db



@pytest.fixture
def stored(client, cfg, make_clip):
    """One clip whose bytes and thumbnail actually exist on disk."""
    clip = make_clip("aB3xY9z", created_at=1757260800)
    db.insert_clip(client.app.state.conn, clip)

    media = cfg.data_dir / clip.rel_path
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"0123456789")

    thumb = cfg.data_dir / clip.thumb_path
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpegbytes")

    return clip


def test_media_serves_the_file_inline(client, stored):
    response = client.get(f"/m/{stored.id}")

    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert response.headers["content-type"] == "video/mp4"
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_media_supports_range_requests(client, stored):
    """Without 206 the player cannot seek — the whole reason /m exists."""
    response = client.get(f"/m/{stored.id}", headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"


def test_media_advertises_range_support(client, stored):
    response = client.get(f"/m/{stored.id}")
    assert response.headers.get("accept-ranges") == "bytes"


def test_thumb_serves_the_thumbnail(client, stored):
    response = client.get(f"/t/{stored.id}")

    assert response.status_code == 200
    assert response.content == b"jpegbytes"
    assert response.headers["content-type"] == "image/jpeg"


def test_download_sets_an_attachment_disposition(client, stored):
    response = client.get(f"/d/{stored.id}")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "halo-aB3xY9z.mp4" in disposition


def test_unknown_id_is_404_on_every_binary_route(client):
    for path in ("/m/nope123", "/t/nope123", "/d/nope123"):
        assert client.get(path).status_code == 404


def test_missing_file_on_disk_is_404_not_500(client, make_clip):
    """A row whose bytes the retention sweep already removed, or a half-restored
    backup. The index is not proof the file is there."""
    db.insert_clip(client.app.state.conn, make_clip("ghost12", created_at=100))
    assert client.get("/m/ghost12").status_code == 404


def test_thumbless_clip_is_404_on_the_thumb_route(client, cfg, make_clip):
    """thumb_path is NULL when ffmpeg failed at ingest. That is a normal row."""
    clip = make_clip("nothumb", created_at=100, thumb=False)
    db.insert_clip(client.app.state.conn, clip)
    media = cfg.data_dir / clip.rel_path
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")

    assert client.get("/t/nothumb").status_code == 404
    assert client.get(f"/m/{clip.id}").status_code == 200


def test_screenshot_is_served_with_its_own_media_type(client, cfg, make_clip):
    clip = make_clip("shot123", created_at=100, kind="screenshot",
                     filename="shot123.png",
                     rel_path="shots/halo/2026/09/shot123.png")
    db.insert_clip(client.app.state.conn, clip)
    path = cfg.data_dir / clip.rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")

    response = client.get("/m/shot123")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
