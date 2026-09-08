"""POST a queued capture to clipd's /ingest.

Retry policy: a 4xx will fail identically forever, so it is permanent and the
job is dropped. A 5xx or a transport error means the server or the tailnet is
having a moment, so the job stays queued — that is plan.md §8.7's "never lose
a clip because the server was unreachable".
"""
from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config
from .jobs import Job

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0  # a several-hundred-MB clip over the tailnet

# 4xx is usually permanent, but these are transient or operator-fixable: a
# token not yet set or just rotated, a proxy redirect, a rate limit. Dropping
# a capture on the first one of these would be a real loss.
RETRYABLE_STATUSES = {401, 403, 408, 425, 429}


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    url: str | None
    clip_id: str | None
    retryable: bool


def _failure(retryable: bool) -> UploadResult:
    return UploadResult(ok=False, url=None, clip_id=None, retryable=retryable)


def upload(cfg: Config, job: Job, client: httpx.Client | None = None) -> UploadResult:
    path = Path(job.path)
    if not path.exists():
        log.error("queued file is gone: %s", path)
        return _failure(retryable=False)

    meta = {
        "kind": job.kind,
        "capture_uuid": job.capture_uuid,
        "game": job.game,
        "game_exe": job.game_exe,
        "source_host": cfg.source_host,
        "captured_at": job.captured_at,
    }
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        with path.open("rb") as handle:
            response = client.post(
                f"{cfg.server_url}/ingest",
                headers={"Authorization": f"Bearer {cfg.ingest_token}"},
                files={"file": (path.name, handle, content_type)},
                data={"meta": json.dumps(meta)},
            )
    except (httpx.HTTPError, OSError):
        log.warning("upload transport failure for %s", job.capture_uuid, exc_info=True)
        return _failure(retryable=True)
    finally:
        if owned:
            client.close()

    if response.status_code == 200:
        try:
            payload = response.json()
            return UploadResult(ok=True, url=payload["url"],
                                clip_id=payload["id"], retryable=False)
        except (ValueError, KeyError):
            log.warning("unparseable 200 from /ingest for %s", job.capture_uuid)
            return _failure(retryable=True)

    status = response.status_code
    retryable = (
        status >= 500 or status in RETRYABLE_STATUSES or 300 <= status < 400
    )
    # Log the body: a bare status makes a 422 from the server's metadata
    # validation impossible to diagnose.
    log.warning("ingest returned %s for %s (retryable=%s): %s",
                status, job.capture_uuid, retryable, response.text[:300])
    return _failure(retryable=retryable)
