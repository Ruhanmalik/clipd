"""Regression tests for defects found in code review of the watcher.

Each test names the failure it locks out. All were independently reproduced
before being fixed.
"""
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from clipwatch.detector import ExeRingBuffer
from clipwatch.platform import NullAdapter as Adapter
from clipwatch.pipeline import drain, reconcile
from clipwatch.uploader import UploadResult
from conftest import make_video, needs_ffmpeg


def add_job(queue, tmp_path, name="a.mp4"):
    f = tmp_path / name
    f.write_bytes(b"videodata")
    return queue.enqueue(path=f, kind="clip", game="Halo", game_exe="halo.exe",
                         captured_at=1757260800, source_path=f)


# 1 — next_attempt_at was persisted from time.monotonic(), which on Windows is
# milliseconds since boot. A job rescheduled after days of uptime became
# eligible again only after the machine had been up that long once more.
def test_an_absurd_future_deadline_is_treated_as_ready(queue, tmp_path):
    # Defence in depth: a queue file written by an older build, or a clock jump.
    job = add_job(queue, tmp_path)
    from dataclasses import replace
    queue._write(replace(job, next_attempt_at=time.time() + 10 * 365 * 86400))
    assert queue.ready(now=time.time()), "implausible deadline must not strand a job"


# 3 — the watchdog thread and the drain thread both called drain(), so the same
# job could be uploaded twice and both would race on the queue file.
def test_concurrent_drains_upload_a_job_only_once(cfg, queue, tmp_path):
    add_job(queue, tmp_path)
    uploaded = []

    def slow_upload(config, job):
        uploaded.append(job.capture_uuid)
        time.sleep(0.3)
        return UploadResult(True, "http://midget:8000/v/abc", "abc", False)

    threads = [
        threading.Thread(target=drain, args=(cfg, queue, Adapter(), 0),
                         kwargs={"upload_fn": slow_upload})
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(uploaded) == 1, f"uploaded {len(uploaded)} times"


# 4 — the sampler thread mutated the deque while a capture iterated it.
def test_ring_buffer_is_safe_under_concurrent_access():
    buf = ExeRingBuffer(window_s=0.05)
    errors = []

    def writer():
        for i in range(20_000):
            try:
                buf.record(f"g{i % 3}.exe", time.monotonic())
            except Exception as exc:
                errors.append(repr(exc))

    def reader():
        for _ in range(20_000):
            try:
                buf.dominant(frozenset(), time.monotonic())
            except Exception as exc:
                errors.append(repr(exc))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors[:3]


def test_ring_buffer_titles_do_not_grow_without_bound():
    buf = ExeRingBuffer(window_s=1)
    for i in range(500):
        buf.record(f"game{i}.exe", i, title=f"Game {i}")
    assert len(buf.titles) < 50, f"titles held {len(buf.titles)} entries"


# 5 — `-c copy` without `-map 0` keeps one stream per type, silently discarding
# OBS's extra audio tracks while reporting success.
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_remux_keeps_every_audio_track(tmp_path):
    from clipwatch.remux import remux_to_mp4

    src = tmp_path / "multi.mkv"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-f", "lavfi", "-i", "sine=frequency=880",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src),
    ], check=True)

    dest = tmp_path / "multi.mp4"
    assert remux_to_mp4(src, dest) is True

    def audio_tracks(path):
        out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                              "-show_streams", str(path)],
                             capture_output=True, text=True).stdout
        return [s for s in json.loads(out)["streams"] if s["codec_type"] == "audio"]

    assert len(audio_tracks(dest)) == len(audio_tracks(src)) == 2


# 6 — +faststart is mandatory (plan.md §10) but only .mkv was remuxed, so an
# OBS configured to output mp4 uploaded a file with moov at the end.
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_an_mp4_input_still_gets_faststart(cfg, tmp_path):
    from clipwatch.pipeline import prepare_capture

    # ffmpeg's mp4 muxer writes moov at the END by default — precisely the
    # "OBS configured to record mp4" case that used to skip the remux.
    src = tmp_path / "recording.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=30", "-t", "1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src),
    ], check=True)
    original = src.read_bytes()[:200_000]
    assert original.index(b"mdat") < original.index(b"moov"), "input should be moov-last"

    job = prepare_capture(cfg, src, ExeRingBuffer(90), {}, now=0)
    assert job is not None
    head = Path(job.path).read_bytes()[:200_000]
    assert head.index(b"moov") < head.index(b"mdat")


# 2 — nothing ever revisited watch_dir or work_dir, so any capture that failed
# to enqueue (daemon down, unstable file, remux failure) was abandoned.
def test_reconcile_picks_up_a_capture_left_while_the_daemon_was_down(cfg, queue, tmp_path):
    orphan = tmp_path / "missed.mp4"
    orphan.write_bytes(b"videodata")

    found = reconcile(cfg, ExeRingBuffer(90), {}, now=0)

    assert found == 1
    assert [Path(j.source_path).name for j in queue.all()] == ["missed.mp4"]


def test_reconcile_does_not_requeue_an_already_queued_capture(cfg, queue, tmp_path):
    reconcile(cfg, ExeRingBuffer(90), {}, now=0)
    (tmp_path / "one.mp4").write_bytes(b"x")
    reconcile(cfg, ExeRingBuffer(90), {}, now=0)
    before = len(queue.all())

    reconcile(cfg, ExeRingBuffer(90), {}, now=0)
    assert len(queue.all()) == before


def test_reconcile_adopts_a_genuinely_stale_work_orphan(cfg, queue, tmp_path):
    """A crash between remux and enqueue strands an MP4 in work_dir. Once it is
    older than any plausible in-flight remux, it must be adopted rather than
    abandoned — as Unknown, since its game is unrecoverable."""
    import os
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    stray = cfg.work_dir / "stray.mp4"
    stray.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(stray, (old, old))

    assert reconcile(cfg, ExeRingBuffer(90), {}, now=0) == 1
    jobs = queue.all()
    assert len(jobs) == 1 and jobs[0].game is None


def test_permanently_rejected_captures_move_out_of_the_watch_dir(cfg, queue, tmp_path):
    job = add_job(queue, tmp_path, "bad.mp4")

    def rejected(config, j):
        return UploadResult(False, None, None, retryable=False)

    drain(cfg, queue, Adapter(), now=0, upload_fn=rejected)

    assert not Path(job.path).exists(), "rejected file must leave the watch dir"
    moved = list(cfg.rejected_dir.glob("*.mp4"))
    assert len(moved) == 1, "rejected file must be preserved, not deleted"
    # ...and must not be picked straight back up.
    assert reconcile(cfg, ExeRingBuffer(90), {}, now=0) == 0


def test_a_rejected_capture_notifies_the_user(cfg, queue, tmp_path):
    add_job(queue, tmp_path, "bad.mp4")
    adapter = Adapter()

    drain(cfg, queue, adapter, now=0,
          upload_fn=lambda c, j: UploadResult(False, None, None, retryable=False))

    assert adapter.notifications, "a dropped capture must not be silent"


@needs_ffmpeg
def test_reconcile_does_not_adopt_its_own_remux_output(cfg, queue, tmp_path):
    """The watch_dir pass creates work_dir output; the work_dir pass must not
    then treat that output as an orphan and queue the same capture twice.

    Uses a real video so the remux actually succeeds — with a stub payload the
    remux fails, work_dir stays empty, and the path under test is never entered.
    """
    make_video(tmp_path / "capture.mkv")

    adopted = reconcile(cfg, ExeRingBuffer(90), {}, now=0)

    assert adopted == 1, f"adopted {adopted} for a single capture"
    jobs = queue.all()
    assert len(jobs) == 1
    assert Path(jobs[0].path).parent == cfg.work_dir  # the remux really happened


def test_reconcile_ignores_work_files_that_are_still_in_flight(cfg, queue, tmp_path):
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    (cfg.work_dir / "inflight.mp4").write_bytes(b"x")  # mtime = now

    assert reconcile(cfg, ExeRingBuffer(90), {}, now=0) == 0
    assert queue.all() == []


def test_the_same_capture_is_never_queued_twice(cfg, queue, tmp_path):
    """The watchdog and the reconcile sweep can both reach one file; on macOS
    FSEvents also replays events for pre-existing files. Two enqueues would
    upload one capture under two capture_uuids, defeating server dedupe."""
    from clipwatch.pipeline import prepare_capture

    capture = tmp_path / "replay.mp4"
    capture.write_bytes(b"videodata")

    first = prepare_capture(cfg, capture, ExeRingBuffer(90), {}, now=0)
    second = prepare_capture(cfg, capture, ExeRingBuffer(90), {}, now=0)

    assert first is not None
    assert second is None
    assert len(queue.all()) == 1
