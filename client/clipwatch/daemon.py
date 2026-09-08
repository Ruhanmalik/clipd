"""Wire the pieces together: watch, sample, drain."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .detector import ExeRingBuffer, record_sample
from .jobs import JobQueue
from .pipeline import drain, prepare_capture, reconcile

log = logging.getLogger(__name__)

DRAIN_INTERVAL_S = 5.0
RECONCILE_INTERVAL_S = 300.0

# Two clocks, deliberately. The ring buffer measures elapsed time and must not
# be disturbed by an NTP correction, so it uses monotonic. Queue deadlines are
# written to disk and must outlive a reboot, so they use the wall clock.


class _CaptureHandler(FileSystemEventHandler):
    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon

    def on_created(self, event):
        if not event.is_directory:
            self.daemon.handle_capture(Path(event.src_path))

    def on_moved(self, event):
        # OBS renames its temp file into place on some configurations.
        if not event.is_directory:
            self.daemon.handle_capture(Path(event.dest_path))


class Daemon:
    def __init__(self, cfg: Config, adapter, mapping: dict[str, str]) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.mapping = mapping
        self.buffer = ExeRingBuffer(cfg.detect_window_s)
        self.queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
        self._stop = threading.Event()
        self._observer = Observer()
        self._threads: list[threading.Thread] = []

    def handle_capture(self, path: Path) -> None:
        try:
            if prepare_capture(self.cfg, path, self.buffer, self.mapping,
                               self.adapter, time.monotonic()):
                drain(self.cfg, self.queue, self.adapter, time.time())
        except Exception:
            log.exception("failed to handle capture %s", path)

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            try:
                record_sample(self.buffer, self.adapter, time.monotonic())
            except Exception:
                log.debug("sampler hiccup", exc_info=True)
            self._stop.wait(self.cfg.sample_interval_s)

    def _drain_loop(self) -> None:
        # Also picks up anything left queued by a previous run.
        while not self._stop.is_set():
            try:
                drain(self.cfg, self.queue, self.adapter, time.time())
            except Exception:
                log.exception("drain failed; will retry")
            self._stop.wait(DRAIN_INTERVAL_S)

    def _reconcile_loop(self) -> None:
        """Adopt captures that never reached the queue.

        A watchdog event is the only other way in, so without this anything
        written while the daemon was down is lost — which plan.md §8.7 forbids.
        """
        while not self._stop.is_set():
            try:
                if reconcile(self.cfg, self.buffer, self.mapping,
                             self.adapter, time.monotonic()):
                    drain(self.cfg, self.queue, self.adapter, time.time())
            except Exception:
                log.exception("reconcile failed; will retry")
            self._stop.wait(RECONCILE_INTERVAL_S)

    def start(self) -> None:
        for directory in (self.cfg.work_dir, self.cfg.queue_dir, self.cfg.rejected_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self._observer.schedule(_CaptureHandler(self), str(self.cfg.watch_dir),
                                recursive=False)
        self._observer.start()

        for target in (self._sample_loop, self._drain_loop,
                       self._reconcile_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

        log.info("watching %s, uploading to %s", self.cfg.watch_dir, self.cfg.server_url)

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()
        self._observer.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
