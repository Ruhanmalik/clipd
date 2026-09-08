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
        """Atomic: a half-written job file must never be loadable."""
        target = self._file(job.capture_uuid)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(job), indent=2))
        os.replace(tmp, target)

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
        return [j for j in self.all() if j.next_attempt_at <= now]

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
