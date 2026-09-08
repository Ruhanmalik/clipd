"""Environment-backed configuration. Loaded once at startup."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?B?)\s*$", re.IGNORECASE)

# Binary multipliers: "50GB" means 50 GiB. The disk budget on midget is what
# matters here, and `df` reports binary units, so matching it avoids a 7% surprise.
_MULTIPLIERS = {
    "": 1, "B": 1,
    "K": 1024, "KB": 1024,
    "M": 1024**2, "MB": 1024**2,
    "G": 1024**3, "GB": 1024**3,
    "T": 1024**4, "TB": 1024**4,
}


def parse_size(text: str) -> int:
    """Parse '50GB', '512mb', or a plain byte count into an int."""
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"cannot parse size: {text!r}")
    number, suffix = match.groups()
    return int(float(number) * _MULTIPLIERS[suffix.upper()])


# A budget of 0 would make every non-empty store "over budget", so the first
# sweep would delete the entire unpinned library. A blanked env var must not
# be able to do that.
MIN_STORE_BYTES = 1

DEFAULT_MAX_STORE = "50GB"
DEFAULT_MAX_UPLOAD = "2GB"
DEFAULT_BASE_URL = "http://localhost:8000"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    ingest_token: str
    ntfy_topic: str | None
    ntfy_server: str
    max_store_bytes: int
    base_url: str
    sweep_interval_s: int
    share_enabled: bool
    max_upload_bytes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        token = env.get("INGEST_TOKEN", "").strip()
        if not token:
            raise ValueError("INGEST_TOKEN must be set and non-empty")
        return cls(
            data_dir=Path(env.get("DATA_DIR", "/data")),
            ingest_token=token,
            ntfy_topic=env.get("NTFY_TOPIC") or None,
            ntfy_server=env.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            max_store_bytes=_store_budget(env),
            base_url=(env.get("BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            sweep_interval_s=int(env.get("SWEEP_INTERVAL_S") or "900"),
            share_enabled=env.get("SHARE_ENABLED", "").lower() in {"1", "true", "yes"},
            max_upload_bytes=parse_size(
                env.get("MAX_UPLOAD_BYTES") or DEFAULT_MAX_UPLOAD
            ),
        )


def _store_budget(env: Mapping[str, str]) -> int:
    """Read MAX_STORE_BYTES, refusing a budget that would empty the store."""
    budget = parse_size(env.get("MAX_STORE_BYTES") or DEFAULT_MAX_STORE)
    if budget < MIN_STORE_BYTES:
        raise ValueError(
            f"MAX_STORE_BYTES must be at least {MIN_STORE_BYTES} bytes; got {budget}"
        )
    return budget
