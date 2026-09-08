"""Tests for the daemon's wiring.

daemon.py had no coverage at all, which is how a can't-fail regression test
for the clock choice slipped through: the decision it guards is made here.
"""
import time
from pathlib import Path

import pytest

from clipwatch.config import Config
from clipwatch.daemon import Daemon
from clipwatch.platform import NullAdapter


@pytest.fixture
def cfg(tmp_path):
    watch = tmp_path / "obs"
    watch.mkdir()
    f = tmp_path / "config.toml"
    f.write_text(f'server_url = "http://midget:8000"\nwatch_dir = "{watch.as_posix()}"\n'
                 'source_host = "desktop-amtr56i"\nsample_interval_s = 0.01\n')
    return Config.load(f, {"CLIPD_TOKEN": "t"})


def test_drain_is_scheduled_on_the_wall_clock_not_monotonic(cfg, monkeypatch, tmp_path):
    """Queue deadlines are persisted, so they must be wall-clock.

    time.monotonic() is time-since-boot on Windows: a deadline written after
    days of uptime would never be reached again after a reboot. Asserting on
    the value the daemon actually passes is the only way to pin that down —
    a test that supplies `now` itself cannot.
    """
    seen = []
    monkeypatch.setattr("clipwatch.daemon.drain",
                        lambda c, q, a, now, **kw: seen.append(now) or 0)

    daemon = Daemon(cfg, NullAdapter(), {})
    daemon.start()
    try:
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.05)
    finally:
        daemon.stop()

    assert seen, "the drain loop never ran"
    # Wall clock is ~1.7e9; monotonic on macOS/Windows is uptime, orders of
    # magnitude smaller. Allow a generous window rather than an exact match.
    assert abs(seen[0] - time.time()) < 60, (
        f"drain received now={seen[0]!r}, which is not a wall-clock time"
    )


def test_capture_handling_also_drains_on_the_wall_clock(cfg, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr("clipwatch.daemon.drain",
                        lambda c, q, a, now, **kw: seen.append(now) or 0)
    monkeypatch.setattr("clipwatch.daemon.prepare_capture",
                        lambda *a, **kw: object())

    daemon = Daemon(cfg, NullAdapter(), {})
    daemon.handle_capture(Path("whatever.mkv"))

    assert seen and abs(seen[0] - time.time()) < 60


def test_the_ring_buffer_is_sampled_on_the_monotonic_clock(cfg, monkeypatch):
    """The reverse of the above: elapsed-time measurement must not be
    disturbed by an NTP correction, so the sampler uses monotonic."""
    seen = []
    monkeypatch.setattr("clipwatch.daemon.record_sample",
                        lambda buf, adapter, now: seen.append(now))

    daemon = Daemon(cfg, NullAdapter(), {})
    daemon.start()
    try:
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.05)
    finally:
        daemon.stop()

    assert seen, "the sampler never ran"
    assert seen[0] < 1_000_000_000, (
        f"sampler received now={seen[0]!r}, which looks like a wall-clock time"
    )


def test_start_creates_every_state_directory(cfg):
    daemon = Daemon(cfg, NullAdapter(), {})
    daemon.start()
    try:
        assert cfg.work_dir.is_dir()
        assert cfg.queue_dir.is_dir()
        assert cfg.rejected_dir.is_dir()
    finally:
        daemon.stop()


def test_a_failing_capture_does_not_kill_the_daemon(cfg, monkeypatch):
    def explode(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("clipwatch.daemon.prepare_capture", explode)
    daemon = Daemon(cfg, NullAdapter(), {})
    daemon.handle_capture(Path("x.mkv"))  # must not raise


def test_stop_is_idempotent(cfg):
    daemon = Daemon(cfg, NullAdapter(), {})
    daemon.start()
    daemon.stop()
    daemon.stop()
