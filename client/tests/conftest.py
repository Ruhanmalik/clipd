"""Shared fixtures.

test_pipeline, test_hardening and test_uploader all need the same thing: a
Config over a temp watch dir, a queue on it, and canned upload outcomes.
"""
import shutil
import subprocess

import pytest

from clipwatch.config import Config
from clipwatch.jobs import JobQueue
from clipwatch.uploader import UploadResult

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg required"
)


@pytest.fixture
def cfg(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        f'server_url = "http://midget:8000"\n'
        f'watch_dir = "{tmp_path.as_posix()}"\n'
        'source_host = "desktop-amtr56i"\n'
        'stability_checks = 1\n'
        'stability_interval_s = 0.001\n'
    )
    return Config.load(f, {"CLIPD_TOKEN": "secret-token"})


@pytest.fixture
def queue(cfg):
    return JobQueue(cfg.queue_dir, cfg.max_backoff_s)


def make_video(path, *, seconds="1", audio_tracks=1, size="320x240"):
    """A real H.264 file, so remux tests exercise ffmpeg rather than a stub."""
    inputs = ["-f", "lavfi", "-i", f"testsrc=size={size}:rate=30"]
    maps = ["-map", "0:v"]
    for i in range(audio_tracks):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={440 * (i + 1)}"]
        maps += ["-map", f"{i + 1}:a"]
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs, *maps, "-t", seconds,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def sample_mkv(tmp_path):
    return make_video(tmp_path / "in.mkv")


def uploads_ok(url="http://midget:8000/v/abc"):
    return lambda cfg, job: UploadResult(True, url, "abc", False)


def upload_unavailable():
    return lambda cfg, job: UploadResult(False, None, None, retryable=True)


def upload_refused():
    return lambda cfg, job: UploadResult(False, None, None, retryable=False)
