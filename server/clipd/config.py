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
            max_store_bytes=parse_size(env.get("MAX_STORE_BYTES", "50GB")),
            base_url=env.get("BASE_URL", "http://localhost:8000").rstrip("/"),
            sweep_interval_s=int(env.get("SWEEP_INTERVAL_S", "900")),
            share_enabled=env.get("SHARE_ENABLED", "").lower() in {"1", "true", "yes"},
        )
