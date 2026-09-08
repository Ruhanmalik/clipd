"""One capture, end to end: settle, remux, enqueue, upload, clean up."""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .detector import ExeRingBuffer
from .games import resolve_game
from .jobs import Job, JobQueue
from .remux import KIND_FOR_SUFFIX, needs_remux, remux_to_mp4
from .stability import wait_until_stable
from .uploader import upload

log = logging.getLogger(__name__)

STABILITY_TIMEOUT_S = 120.0


def prepare_capture(
    cfg: Config, path: Path, buffer: ExeRingBuffer, mapping: dict[str, str],
    adapter, now: float,
) -> Job | None:
    """Settle, remux if needed, and enqueue. None if the file is not ours."""
    path = Path(path)

    # Our own remux output lands in work_dir; re-ingesting it would loop.
    if cfg.work_dir in path.parents or cfg.queue_dir in path.parents:
        return None

    kind = KIND_FOR_SUFFIX.get(path.suffix.lower())
    if kind is None:
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
        upload_path = cfg.work_dir / f"{path.stem}.mp4"
        if not remux_to_mp4(path, upload_path):
            log.error("remux failed, not enqueuing %s", path)
            return None

    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    job = queue.enqueue(
        path=upload_path, kind=kind, game=game, game_exe=exe,
        captured_at=captured_at, source_path=path,
    )
    log.info("queued %s as %s (%s)", path.name, job.capture_uuid, game)
    return job


def drain(cfg: Config, queue: JobQueue, adapter, now: float, upload_fn=upload) -> int:
    """Attempt every ready job. Returns how many succeeded."""
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
            # Permanent rejection. Drop the job but KEEP the file: retrying a
            # 4xx forever would fill the disk, while deleting a capture the
            # server refused would lose it for good.
            log.error("dropping permanently rejected job %s (file kept at %s)",
                      job.capture_uuid, job.path)
            queue.done(job)

    return succeeded
