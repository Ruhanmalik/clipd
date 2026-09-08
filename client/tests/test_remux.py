import json
import shutil
import subprocess
import pytest
from pathlib import Path
from clipwatch.remux import KIND_FOR_SUFFIX, needs_remux, remux_to_mp4

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


@pytest.fixture
def sample_mkv(tmp_path):
    out = tmp_path / "in.mkv"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=30", "-t", "2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
    ], check=True)
    return out


def stream_md5(path):
    """md5 of the copied video stream — identical iff nothing was re-encoded."""
    return subprocess.run([
        "ffmpeg", "-v", "quiet", "-i", str(path),
        "-map", "0:v", "-c", "copy", "-f", "md5", "-",
    ], capture_output=True, text=True, check=True).stdout.strip()


def probe(path):
    raw = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], capture_output=True, text=True, check=True).stdout
    return json.loads(raw)


def test_every_clip_container_is_remuxed_but_screenshots_are_not():
    # plan.md §10 makes +faststart mandatory, so an MP4 or MOV recording gets
    # the -c copy pass too rather than being uploaded moov-last.
    assert needs_remux(Path("a.mkv")) is True
    assert needs_remux(Path("a.MKV")) is True
    assert needs_remux(Path("a.mp4")) is True
    assert needs_remux(Path("a.mov")) is True
    assert needs_remux(Path("a.png")) is False
    assert needs_remux(Path("a.jpg")) is False


def test_kind_mapping_covers_clips_and_screenshots():
    assert KIND_FOR_SUFFIX[".mkv"] == "clip"
    assert KIND_FOR_SUFFIX[".mp4"] == "clip"
    assert KIND_FOR_SUFFIX[".png"] == "screenshot"


def test_remux_produces_a_playable_mp4(tmp_path, sample_mkv):
    dest = tmp_path / "out.mp4"
    assert remux_to_mp4(sample_mkv, dest) is True
    assert dest.exists() and dest.stat().st_size > 0
    assert probe(dest)["format"]["format_name"].startswith("mov,mp4")


def test_remux_does_not_re_encode(tmp_path, sample_mkv):
    # The point of -c copy: the encoded video stream must be bit-identical.
    # Comparing the stream md5 proves that; comparing codec/width/height only
    # proves a re-encode kept the same settings.
    dest = tmp_path / "out.mp4"
    remux_to_mp4(sample_mkv, dest)
    assert stream_md5(dest) == stream_md5(sample_mkv)

    before = probe(sample_mkv)["streams"][0]
    after = probe(dest)["streams"][0]
    assert after["codec_name"] == before["codec_name"] == "h264"
    assert (after["width"], after["height"]) == (before["width"], before["height"])


def test_remux_sets_faststart(tmp_path, sample_mkv):
    # plan.md §10: without +faststart, playback waits on a full download.
    # With it, the moov atom precedes the mdat payload.
    dest = tmp_path / "out.mp4"
    remux_to_mp4(sample_mkv, dest)
    head = dest.read_bytes()[:200_000]
    assert head.index(b"moov") < head.index(b"mdat")


def test_remux_returns_false_on_a_junk_input(tmp_path):
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video")
    assert remux_to_mp4(junk, tmp_path / "out.mp4") is False


def test_remux_leaves_no_partial_output_on_failure(tmp_path):
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video")
    dest = tmp_path / "out.mp4"
    remux_to_mp4(junk, dest)
    assert not dest.exists()
