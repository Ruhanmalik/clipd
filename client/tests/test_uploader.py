import httpx
import pytest
from pathlib import Path
from clipwatch.config import Config
from clipwatch.jobs import Job
from clipwatch.uploader import UploadResult, upload


@pytest.fixture
def cfg(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(f'server_url = "http://midget:8000"\nwatch_dir = "{tmp_path.as_posix()}"\n'
                    'source_host = "desktop-amtr56i"\n')
    return Config.load(toml, {"CLIPD_TOKEN": "secret-token"})


@pytest.fixture
def job(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"videodata")
    return Job(capture_uuid="uuid-1", path=str(f), kind="clip", game="Counter-Strike 2",
               game_exe="cs2.exe", captured_at=1757260800, attempts=0,
               next_attempt_at=0.0, source_path=str(f))


def transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_successful_upload_returns_the_url(cfg, job):
    def handler(request):
        return httpx.Response(200, json={"id": "aB3xY9z", "url": "http://midget:8000/v/aB3xY9z"})

    result = upload(cfg, job, client=transport(handler))
    assert result == UploadResult(ok=True, url="http://midget:8000/v/aB3xY9z",
                                  clip_id="aB3xY9z", retryable=False)


def test_request_carries_bearer_token_and_metadata(cfg, job):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "x", "url": "u"})

    upload(cfg, job, client=transport(handler))
    assert seen["auth"] == "Bearer secret-token"
    body = seen["body"]
    assert b'"capture_uuid": "uuid-1"' in body or b'"capture_uuid":"uuid-1"' in body
    assert b"videodata" in body


def test_duplicate_response_is_still_success(cfg, job):
    # The server dedupes on capture_uuid; a duplicate means it is safely stored.
    def handler(request):
        return httpx.Response(200, json={"id": "aB3xY9z", "url": "u", "duplicate": True})

    assert upload(cfg, job, client=transport(handler)).ok is True


def test_server_error_is_retryable(cfg, job):
    def handler(request):
        return httpx.Response(503, text="down")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is True


def test_transport_failure_is_retryable(cfg, job):
    def handler(request):
        raise httpx.ConnectError("tailnet down")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is True


@pytest.mark.parametrize("status", [400, 401, 413, 422])
def test_client_errors_are_not_retryable(cfg, job, status):
    # Retrying a rejected request forever would just fill the disk.
    def handler(request):
        return httpx.Response(status, json={"detail": "nope"})

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is False


def test_missing_file_is_not_retryable(cfg, job, tmp_path):
    Path(job.path).unlink()

    def handler(request):
        raise AssertionError("must not attempt a request for a missing file")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is False


def test_malformed_success_body_is_retryable(cfg, job):
    def handler(request):
        return httpx.Response(200, text="not json")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is True
