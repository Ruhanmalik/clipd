"""ntfy push. One topic per service, per plan.md §4."""
from __future__ import annotations

import logging

import httpx

from .config import Config

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable: the TB branch always returns")


async def _post(cfg: Config, headers: dict[str, str], body: str) -> bool:
    if not cfg.ntfy_topic:
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(
                f"{cfg.ntfy_server}/{cfg.ntfy_topic}",
                content=body,
                headers=headers,
            )
            response.raise_for_status()
        return True
    except Exception:
        # Never propagate: a push failure must not fail the work that triggered it.
        log.warning("ntfy push failed", exc_info=True)
        return False


async def notify_capture(cfg: Config, *, title: str, body: str, click_url: str) -> bool:
    return await _post(
        cfg,
        {"Title": title, "Click": click_url, "Tags": "clapper"},
        body,
    )


async def notify_retention(
    cfg: Config, *, title: str, body: str, priority: str = "default"
) -> bool:
    return await _post(
        cfg,
        {"Title": title, "Priority": priority, "Tags": "floppy_disk"},
        body,
    )
