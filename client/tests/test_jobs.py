import json
import pytest
from clipwatch.jobs import Job, JobQueue, backoff_for


@pytest.fixture
def queue(tmp_path):
    return JobQueue(tmp_path / "queue", max_backoff_s=300)


def enqueue(queue, tmp_path, name="a.mp4", **over):
    path = tmp_path / name
    path.write_bytes(b"data")
    kwargs = dict(path=path, kind="clip", game="Halo", game_exe="halo.exe",
                  captured_at=1757260800, source_path=path)
    kwargs.update(over)
    return queue.enqueue(**kwargs)


def test_enqueue_mints_a_capture_uuid(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    assert job.capture_uuid and len(job.capture_uuid) >= 32


def test_each_enqueue_gets_a_distinct_uuid(queue, tmp_path):
    a = enqueue(queue, tmp_path, "a.mp4")
    b = enqueue(queue, tmp_path, "b.mp4")
    assert a.capture_uuid != b.capture_uuid


def test_jobs_survive_a_restart(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    reopened = JobQueue(queue.queue_dir, max_backoff_s=300)
    assert [j.capture_uuid for j in reopened.all()] == [job.capture_uuid]


def test_reschedule_keeps_the_same_uuid(queue, tmp_path):
    # The whole point: a retry must reuse the key so the server dedupes it.
    job = enqueue(queue, tmp_path)
    again = queue.reschedule(job, now=100)
    assert again.capture_uuid == job.capture_uuid
    assert again.attempts == 1


def test_reschedule_persists_across_a_restart(queue, tmp_path):
    job = queue.reschedule(enqueue(queue, tmp_path), now=100)
    reopened = JobQueue(queue.queue_dir, max_backoff_s=300)
    assert reopened.all()[0].attempts == job.attempts


def test_ready_excludes_jobs_still_in_backoff(queue, tmp_path):
    job = queue.reschedule(enqueue(queue, tmp_path), now=100)
    assert queue.ready(now=100) == []
    assert [j.capture_uuid for j in queue.ready(now=job.next_attempt_at)] == [job.capture_uuid]


def test_a_fresh_job_is_immediately_ready(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    assert [j.capture_uuid for j in queue.ready(now=0)] == [job.capture_uuid]


def test_done_removes_the_job(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    queue.done(job)
    assert queue.all() == []
    assert JobQueue(queue.queue_dir, max_backoff_s=300).all() == []


def test_backoff_grows_then_caps():
    assert backoff_for(1, 300) == 5
    assert backoff_for(2, 300) == 10
    assert backoff_for(3, 300) == 20
    assert backoff_for(20, 300) == 300  # capped


def test_ready_is_oldest_first(queue, tmp_path):
    a = enqueue(queue, tmp_path, "a.mp4", captured_at=100)
    b = enqueue(queue, tmp_path, "b.mp4", captured_at=200)
    assert [j.capture_uuid for j in queue.ready(now=0)] == [a.capture_uuid, b.capture_uuid]


def test_a_corrupt_job_file_is_skipped_not_fatal(queue, tmp_path):
    enqueue(queue, tmp_path)
    (queue.queue_dir / "broken.json").write_text("{ not json")
    assert len(queue.all()) == 1  # the good job still loads
