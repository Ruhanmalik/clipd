"""Durable upload queue.

plan.md §8.7: never lose a clip because the server was unreachable. The PC
may reboot between the capture and a successful upload, so the queue is a
directory of JSON files rather than anything in memory — inspectable with a
text editor when something goes wrong.

capture_uuid is minted once, at enqueue, and carried through every retry.
That is what lets the server collapse duplicates (design §3); regenerating
it per attempt would defeat the entire idempotency design.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

BASE_BACKOFF_S = 5.0


def backoff_for(attempts: int, max_backoff_s: float) -> float:
    """5s, 10s, 20s, 40s ... capped. Attempts are 1-based."""
    return min(BASE_BACKOFF_S * (2 ** max(0, attempts - 1)), max_backoff_s)


@dataclass(frozen=True)
class Job:
    capture_uuid: str
    path: str
    kind: str
    game: str | None
    game_exe: str | None
    captured_at: int
    attempts: int
    next_attempt_at: float
    source_path: str


class JobQueue:
    def __init__(self, queue_dir: Path, max_backoff_s: float) -> None:
        self.queue_dir = Path(queue_dir)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.max_backoff_s = max_backoff_s

    def _file(self, capture_uuid: str) -> Path:
        return self.queue_dir / f"{capture_uuid}.json"

    def _write(self, job: Job) -> None:
        """Atomic and durable: a half-written job file must never be loadable.

        The temp name is unique because two threads may reschedule the same
        job concurrently; a fixed name would let one rename the other's file
        out from under it.
        """
        target = self._file(job.capture_uuid)
        fd, tmp_name = tempfile.mkstemp(dir=self.queue_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(asdict(job), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())  # survive a power cut, not just a crash
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def enqueue(self, *, path: Path, kind: str, game: str | None,
                game_exe: str | None, captured_at: int, source_path: Path) -> Job:
        job = Job(
            capture_uuid=uuid.uuid4().hex,
            path=str(path),
            kind=kind,
            game=game,
            game_exe=game_exe,
            captured_at=captured_at,
            attempts=0,
            next_attempt_at=0.0,
            source_path=str(source_path),
        )
        self._write(job)
        return job

    def all(self) -> list[Job]:
        jobs: list[Job] = []
        for path in sorted(self.queue_dir.glob("*.json")):
            try:
                jobs.append(Job(**json.loads(path.read_text())))
            except (json.JSONDecodeError, TypeError, ValueError):
                # One unreadable file must not stall every other capture.
                log.warning("skipping unreadable job file %s", path)
        return sorted(jobs, key=lambda j: j.captured_at)

    def ready(self, now: float) -> list[Job]:
        """Jobs due now. `now` is wall-clock seconds (time.time()).

        Deadlines are persisted, so they must be wall-clock: time.monotonic()
        is time-since-boot on Windows, and a deadline written after days of
        uptime would never be reached again after a reboot.
        """
        horizon = now + self.max_backoff_s
        # A deadline beyond any backoff we could have scheduled means the clock
        # moved (NTP correction, or a file from a monotonic-scheduling build).
        # Treat it as due rather than stranding the capture forever.
        return [j for j in self.all()
                if j.next_attempt_at <= now or j.next_attempt_at > horizon]

    def reschedule(self, job: Job, now: float) -> Job:
        attempts = job.attempts + 1
        updated = replace(
            job,
            attempts=attempts,
            next_attempt_at=now + backoff_for(attempts, self.max_backoff_s),
        )
        self._write(updated)
        return updated

    def done(self, job: Job) -> None:
        self._file(job.capture_uuid).unlink(missing_ok=True)
