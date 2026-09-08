"""Configuration: a TOML file next to the executable, plus env overrides.

The token is accepted from either, but CLIPD_TOKEN wins — a config file is
easy to screenshot or paste into a support thread, an env var is not.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# design §5. Compared lowercased, because Windows reports executable casing
# inconsistently between processes.
DEFAULT_IGNORE_EXES = frozenset({
    "explorer.exe", "discord.exe", "chrome.exe", "msedge.exe", "firefox.exe",
    "searchhost.exe", "shellexperiencehost.exe",
})


@dataclass(frozen=True)
class Config:
    server_url: str
    ingest_token: str
    watch_dir: Path
    work_dir: Path
    queue_dir: Path
    rejected_dir: Path
    source_host: str
    sample_interval_s: float
    detect_window_s: float
    ignore_exes: frozenset[str]
    stability_checks: int
    stability_interval_s: float
    max_backoff_s: float

    @classmethod
    def load(cls, toml_path: Path | None, env: Mapping[str, str]) -> "Config":
        raw: dict = {}
        if toml_path is not None and toml_path.exists():
            # TOML is defined as UTF-8; the platform default is cp1252
            # on Windows, which mangles a non-ASCII watch_dir.
            raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))

        token = env.get("CLIPD_TOKEN") or raw.get("ingest_token") or ""
        if not token.strip():
            raise ValueError("CLIPD_TOKEN must be set, or ingest_token given in config")

        watch_dir = Path(raw["watch_dir"])
        state = watch_dir / ".clipwatch"

        # `is not None`, so an explicit empty list really disables the list.
        ignore = raw.get("ignore_exes")
        ignore_exes = (
            frozenset(e.lower() for e in ignore)
            if ignore is not None else DEFAULT_IGNORE_EXES
        )

        return cls(
            server_url=str(raw["server_url"]).rstrip("/"),
            ingest_token=token.strip(),
            watch_dir=watch_dir,
            work_dir=Path(raw.get("work_dir", state / "work")),
            queue_dir=Path(raw.get("queue_dir", state / "queue")),
            rejected_dir=Path(raw.get("rejected_dir", state / "rejected")),
            source_host=raw.get("source_host", "unknown"),
            sample_interval_s=float(raw.get("sample_interval_s", 5.0)),
            detect_window_s=float(raw.get("detect_window_s", 90.0)),
            ignore_exes=ignore_exes,
            stability_checks=int(raw.get("stability_checks", 3)),
            stability_interval_s=float(raw.get("stability_interval_s", 0.5)),
            max_backoff_s=float(raw.get("max_backoff_s", 300.0)),
        )
