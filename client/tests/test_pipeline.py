import pytest
from pathlib import Path
from clipwatch.detector import ExeRingBuffer
from clipwatch.platform import NullAdapter
from clipwatch.pipeline import drain, prepare_capture
from clipwatch.uploader import UploadResult
from conftest import make_video, needs_ffmpeg, uploads_ok


@pytest.fixture
def buffer():
    buf = ExeRingBuffer(window_s=90)
    for t in range(0, 60, 5):
        buf.record("cs2.exe", t, title="Counter-Strike 2")
    return buf


MAPPING = {"cs2.exe": "Counter-Strike 2"}


def make_mp4(tmp_path, name="replay.mp4"):
    f = tmp_path / name
    f.write_bytes(b"videodata")
    return f


def test_prepare_resolves_the_game_from_the_ring_buffer(cfg, tmp_path, buffer):
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, now=60)
    assert job.game == "Counter-Strike 2"
    assert job.game_exe == "cs2.exe"
    assert job.kind == "clip"


def test_prepare_marks_screenshots_as_screenshots(cfg, tmp_path, buffer):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"pngdata")
    job = prepare_capture(cfg, shot, buffer, MAPPING, now=60)
    assert job.kind == "screenshot"


def test_prepare_falls_back_to_unknown_with_no_samples(cfg, tmp_path):
    job = prepare_capture(cfg, make_mp4(tmp_path), ExeRingBuffer(90), MAPPING, now=0)
    assert job.game == "Unknown"


def test_prepare_persists_the_job(cfg, tmp_path, buffer, queue):
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, now=60)
    assert [j.capture_uuid for j in queue.all()] == [job.capture_uuid]


def test_prepare_ignores_our_own_working_files(cfg, tmp_path, buffer):
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    internal = cfg.work_dir / "already.mp4"
    internal.write_bytes(b"x")
    assert prepare_capture(cfg, internal, buffer, MAPPING, now=60) is None


def test_prepare_ignores_unknown_extensions(cfg, tmp_path, buffer):
    other = tmp_path / "notes.txt"
    other.write_bytes(b"x")
    assert prepare_capture(cfg, other, buffer, MAPPING, now=60) is None


def test_drain_deletes_the_local_file_only_after_a_200(cfg, tmp_path, buffer, queue):
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, now=60)
    adapter = NullAdapter()

    assert drain(cfg, queue, adapter, now=0, upload_fn=uploads_ok()) == 1
    assert queue.all() == []
    assert not Path(job.path).exists()
    assert adapter.clipboard == "http://midget:8000/v/abc"


def test_drain_keeps_the_file_when_the_upload_is_retryable(cfg, tmp_path, buffer, queue):
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, now=60)

    def down(config, j):
        return UploadResult(False, None, None, retryable=True)

    assert drain(cfg, queue, NullAdapter(), now=0, upload_fn=down) == 0
    assert Path(job.path).exists()           # never lose a capture
    assert queue.all()[0].attempts == 1      # and it is scheduled to retry


def test_drain_skips_jobs_still_in_backoff(cfg, tmp_path, buffer, queue):
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, now=60)
    queue.reschedule(job, now=1000)

    def explode(config, j):
        raise AssertionError("must not upload a job still in backoff")

    assert drain(cfg, queue, NullAdapter(), now=1000, upload_fn=explode) == 0


def test_drain_notifies_on_success(cfg, tmp_path, buffer, queue):
    prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, now=60)
    adapter = NullAdapter()

    drain(cfg, queue, adapter, now=0, upload_fn=uploads_ok())
    assert adapter.notifications and "Counter-Strike 2" in adapter.notifications[0][0]


@needs_ffmpeg
def test_drain_removes_the_original_mkv_after_success(cfg, tmp_path, buffer, queue):
    # The remuxed mp4 and the OBS original are both local copies; a confirmed
    # 200 is what makes it safe to drop either.
    mkv = make_video(tmp_path / "replay.mkv")

    job = prepare_capture(cfg, mkv, buffer, MAPPING, now=60)
    assert Path(job.path) != mkv  # it was remuxed into work_dir

    drain(cfg, queue, NullAdapter(), now=0, upload_fn=uploads_ok())
    assert not mkv.exists()
    assert not Path(job.path).exists()
