"""One capture, end to end: settle, remux, enqueue, upload, clean up."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from .config import Config
from .detector import ExeRingBuffer
from .games import resolve_game
from .jobs import Job, JobQueue
from .platform.base import PlatformAdapter
from .remux import KIND_FOR_SUFFIX, needs_remux, remux_to_mp4
from .stability import wait_until_stable
from .uploader import upload

log = logging.getLogger(__name__)

STABILITY_TIMEOUT_S = 120.0

# The watchdog thread drains for latency and the drain thread drains on a timer.
# Without this both can claim the same job: the whole clip gets uploaded twice,
# and they race on the queue file.
_DRAIN_LOCK = threading.Lock()

# Reconcile must not overlap itself, or the watch_dir pass of one run adopts
# work_dir output from another.
_RECONCILE_LOCK = threading.Lock()

# A freshly written work_dir MP4 belongs to an in-flight capture, not to a
# crashed one. Only adopt output that has sat unreferenced for longer than any
# plausible remux.
ORPHAN_MIN_AGE_S = 300.0


def queued_sources(cfg: Config) -> set[str]:
    """Every path already represented by a queue entry, source and remuxed."""
    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    paths: set[str] = set()
    for job in queue.all():
        paths.add(job.path)
        paths.add(job.source_path)
    return paths


def reconcile(
    cfg: Config, buffer: ExeRingBuffer, mapping: dict[str, str], now: float
) -> int:
    """Adopt captures that never made it into the queue. Returns how many.

    A watchdog event is the only other way in, so anything written while the
    daemon was down — or dropped by an unstable file, a failed remux, or a
    swallowed exception — would otherwise sit on disk forever. plan.md §8.7
    says never lose a capture, and that has to include these.
    """
    if not _RECONCILE_LOCK.acquire(blocking=False):
        return 0
    try:
        return _reconcile_locked(cfg, buffer, mapping, now)
    finally:
        _RECONCILE_LOCK.release()


def _reconcile_locked(
    cfg: Config, buffer: ExeRingBuffer, mapping: dict[str, str], now: float
) -> int:
    internal = {cfg.work_dir.resolve(), cfg.queue_dir.resolve(),
                cfg.rejected_dir.resolve()}
    adopted = 0

    # Read the queue once for the whole sweep. prepare_capture would otherwise
    # re-read it per file, making a sweep over N captures cost N scans.
    known = queued_sources(cfg)
    for path in sorted(cfg.watch_dir.iterdir()) if cfg.watch_dir.is_dir() else []:
        if not path.is_file() or path.resolve().parent in internal:
            continue
        job = prepare_capture(cfg, path, buffer, mapping, now, known=known)
        if job:
            known.update({job.path, job.source_path})
            adopted += 1

    # An MP4 in work_dir with no job is a crash between remux and enqueue. Its
    # game is unrecoverable, so it lands as Unknown — which design §5 already
    # treats as the re-tagging queue. Better an Unknown clip than a lost one.
    #
    # `known` already includes the remux output of everything queued above,
    # so that output cannot be mistaken for an orphan here.
    if cfg.work_dir.is_dir():
        queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
        for path in sorted(cfg.work_dir.glob("*.mp4")):
            if str(path) in known:
                continue
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age < ORPHAN_MIN_AGE_S:
                continue  # still in flight
            queue.enqueue(path=path, kind="clip", game=None, game_exe=None,
                          captured_at=int(path.stat().st_mtime), source_path=path)
            log.warning("adopted orphaned remux %s as Unknown", path.name)
            adopted += 1

    if adopted:
        log.info("reconcile adopted %d capture(s)", adopted)
    return adopted


def prepare_capture(
    cfg: Config, path: Path, buffer: ExeRingBuffer, mapping: dict[str, str],
    now: float, known: set[str] | None = None,
) -> Job | None:
    """Settle, remux if needed, and enqueue. None if the file is not ours.

    `known` lets a caller that already read the queue avoid a re-read per file.
    """
    path = Path(path)

    # Our own remux output lands in work_dir; re-ingesting it would loop.
    if any(d in path.parents
           for d in (cfg.work_dir, cfg.queue_dir, cfg.rejected_dir)):
        return None

    kind = KIND_FOR_SUFFIX.get(path.suffix.lower())
    if kind is None:
        return None

    # The watchdog and the reconcile sweep can both reach the same file — and
    # on macOS FSEvents will even replay a historical event for a file that
    # existed before the observer started. Enqueuing twice means uploading the
    # same capture under two capture_uuids, which defeats server-side dedupe.
    if str(path) in (known if known is not None else queued_sources(cfg)):
        log.debug("%s is already queued", path.name)
        return None

    if not path.exists():
        # Normal: a replayed filesystem event for a capture already uploaded
        # and cleaned up. Not the same thing as a file that never settled.
        log.debug("%s no longer exists", path.name)
        return None

    if not wait_until_stable(path, checks=cfg.stability_checks,
                             interval_s=cfg.stability_interval_s,
                             timeout_s=STABILITY_TIMEOUT_S):
        log.warning("giving up on unstable file %s", path)
        return None

    exe = buffer.dominant(cfg.ignore_exes, now)
    game = resolve_game(exe, mapping, buffer.titles.get(exe or ""))

    captured_at = int(path.stat().st_mtime)

    upload_path = path
    if needs_remux(path):
        remuxed = cfg.work_dir / f"{path.stem}-{captured_at}.mp4"
        if remux_to_mp4(path, remuxed):
            upload_path = remuxed
        elif path.suffix.lower() == ".mkv":
            # Fatal only for MKV: browsers cannot play it, so an un-remuxed
            # upload would be useless. Leave the file for reconcile to retry.
            log.error("remux failed for %s; leaving it for the next sweep", path)
            return None
        else:
            # The source is already a browser-playable container. Uploading it
            # without the faststart pass is worse than ideal but far better
            # than dropping the capture.
            log.warning("remux failed for %s; uploading the original", path)

    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    job = queue.enqueue(
        path=upload_path, kind=kind, game=game, game_exe=exe,
        captured_at=captured_at, source_path=path,
    )
    log.info("queued %s as %s (%s)", path.name, job.capture_uuid, game)
    return job


def _reject(cfg: Config, job: Job) -> Path | None:
    """Move a permanently refused capture out of the way, preserving it.

    Leaving it in watch_dir would have reconcile pick it up forever; deleting
    it would lose a capture the user can never retake.
    """
    source = Path(job.path)
    if not source.exists():
        return None
    cfg.rejected_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.rejected_dir / f"{job.capture_uuid}{source.suffix}"
    try:
        shutil.move(str(source), str(dest))
        return dest
    except OSError:
        log.warning("could not move rejected capture %s", source, exc_info=True)
        return None


def drain(cfg: Config, queue: JobQueue, adapter: PlatformAdapter,
          now: float, upload_fn=upload) -> int:
    """Attempt every ready job. Returns how many succeeded.

    Non-blocking lock: the immediate drain exists only to cut latency, so if a
    drain is already running there is nothing to gain by queueing behind it.
    """
    if not _DRAIN_LOCK.acquire(blocking=False):
        return 0
    try:
        return _drain_locked(cfg, queue, adapter, now, upload_fn)
    finally:
        _DRAIN_LOCK.release()


def _drain_locked(cfg: Config, queue: JobQueue, adapter: PlatformAdapter,
                  now: float, upload_fn) -> int:
    succeeded = 0

    for job in queue.ready(now):
        result = upload_fn(cfg, job)

        if result.ok:
            # plan.md §8.7: only now is it safe to delete anything local.
            Path(job.path).unlink(missing_ok=True)
            if job.source_path != job.path:
                Path(job.source_path).unlink(missing_ok=True)
            queue.done(job)

            adapter.set_clipboard(result.url or "")
            adapter.notify(job.game or "Unknown", f"uploaded · {result.url}")
            log.info("uploaded %s -> %s", job.capture_uuid, result.url)
            succeeded += 1

        elif result.retryable:
            updated = queue.reschedule(job, now)
            log.warning("retry %s in %.0fs (attempt %d)", job.capture_uuid,
                        updated.next_attempt_at - now, updated.attempts)

        else:
            # Permanent rejection: retrying a 4xx forever would fill the disk,
            # but deleting a capture the user cannot retake would be worse. Move
            # it aside so it is preserved without being re-adopted, and say so —
            # the clipboard and the toast are the entire interface, so a silent
            # drop is indistinguishable from success.
            moved = _reject(cfg, job)
            Path(job.source_path).unlink(missing_ok=True) \
                if job.source_path != job.path else None
            queue.done(job)
            log.error("clipd refused %s; capture preserved at %s",
                      job.capture_uuid, moved or job.path)
            adapter.notify(
                "clipd rejected a capture",
                f"{job.game or 'Unknown'} — kept at {moved or job.path}",
            )

    return succeeded
