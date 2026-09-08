import pytest
from pathlib import Path
from clipd.storage import (
    rel_path_for, thumb_rel_path, write_stream, move_capture, remove_capture,
)


async def chunks_of(*blobs):
    for blob in blobs:
        yield blob


def test_rel_path_for_clip_is_game_then_date():
    path = rel_path_for("clip", "counter-strike-2", 1757260800, "aB3xY9z", ".mp4")
    assert path == "clips/counter-strike-2/2025/09/aB3xY9z.mp4"


def test_rel_path_for_screenshot_uses_shots_root():
    path = rel_path_for("screenshot", "valorant", 1757260800, "xY7z", ".png")
    assert path == "shots/valorant/2025/09/xY7z.png"


def test_rel_path_rejects_unknown_kind():
    with pytest.raises(ValueError):
        rel_path_for("video", "halo", 1757260800, "a", ".mp4")


def test_thumb_rel_path():
    assert thumb_rel_path("aB3xY9z") == "thumbs/aB3xY9z.jpg"


async def test_write_stream_creates_parents_and_returns_size(tmp_path):
    dest = tmp_path / "clips" / "halo" / "2026" / "09" / "a.mp4"
    written = await write_stream(dest, chunks_of(b"abc", b"defg"))
    assert written == 7
    assert dest.read_bytes() == b"abcdefg"


async def test_write_stream_cleans_up_on_failure(tmp_path):
    async def exploding():
        yield b"partial"
        raise IOError("network died")

    dest = tmp_path / "clips" / "halo" / "a.mp4"
    with pytest.raises(IOError):
        await write_stream(dest, exploding())
    # A half-written file must not survive to be indexed as a real clip.
    assert not dest.exists()


async def test_move_capture_relocates_and_prunes_empty_dirs(tmp_path):
    old = "clips/unknown/2026/09/a.mp4"
    new = "clips/halo/2026/09/a.mp4"
    await write_stream(tmp_path / old, chunks_of(b"data"))

    move_capture(tmp_path, old, new)

    assert (tmp_path / new).read_bytes() == b"data"
    assert not (tmp_path / old).exists()
    assert not (tmp_path / "clips" / "unknown").exists()


async def test_remove_capture_deletes_file_and_empty_dirs(tmp_path):
    rel = "clips/halo/2026/09/a.mp4"
    await write_stream(tmp_path / rel, chunks_of(b"data"))

    remove_capture(tmp_path, rel)

    assert not (tmp_path / rel).exists()
    assert not (tmp_path / "clips" / "halo").exists()


def test_remove_capture_is_safe_when_file_is_already_gone(tmp_path):
    remove_capture(tmp_path, "clips/halo/2026/09/missing.mp4")  # must not raise
