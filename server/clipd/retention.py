"""Size-capped retention. No age limit — spec §7.

Radarr and Sonarr consume the same disk (plan.md §6), so clips must not be
the thing that fills it at 3am.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import db, storage
from .config import Config
from .notify import human_bytes, notify_retention

log = logging.getLogger(__name__)

WARN_THRESHOLD = 0.80  # plan.md §6: warn at ~80% of budget


@dataclass(frozen=True)
class SweepResult:
    deleted: int
    freed_bytes: int
    total_bytes: int
    unprunable_bytes: int
    over_budget: bool


@dataclass
class RetentionState:
    """Edge-triggers the alerts so a full store does not push every 15 minutes."""
    warned_high: bool = False
    warned_unprunable: bool = False


def sweep(conn, cfg: Config) -> SweepResult:
    _, total = db.store_totals(conn)
    budget = cfg.max_store_bytes
    deleted = freed = 0

    if total > budget:
        for clip in db.prunable_oldest_first(conn):
            if total <= budget:
                break
            storage.remove_capture(cfg.data_dir, clip.rel_path)
            if clip.thumb_path:
                storage.remove_capture(cfg.data_dir, clip.thumb_path)
            db.delete_clip(conn, clip.id)
            total -= clip.bytes
            freed += clip.bytes
            deleted += 1
            log.info("retention: deleted %s (%s, %s)",
                     clip.id, clip.game_slug, human_bytes(clip.bytes))

    return SweepResult(
        deleted=deleted,
        freed_bytes=freed,
        total_bytes=total,
        unprunable_bytes=db.unprunable_bytes(conn),
        over_budget=total > budget,
    )


async def sweep_and_notify(conn, cfg: Config, state: RetentionState) -> SweepResult:
    result = sweep(conn, cfg)
    budget = cfg.max_store_bytes

    if result.over_budget:
        # Nothing left to prune: everything above budget is pinned or shared.
        # Shout, but keep accepting uploads — plan.md §8.7 guarantees no capture
        # is lost, and dropping one is worse than a temporarily oversized store.
        if not state.warned_unprunable:
            await notify_retention(
                cfg,
                title="clipd store over budget",
                body=(
                    f"{human_bytes(result.total_bytes)} of {human_bytes(budget)} used. "
                    f"{human_bytes(result.unprunable_bytes)} is unprunable "
                    f"(pinned or shared). Still accepting uploads."
                ),
                priority="high",
            )
            state.warned_unprunable = True
    else:
        state.warned_unprunable = False

    ratio = result.total_bytes / budget if budget else 0.0
    if ratio >= WARN_THRESHOLD and not result.over_budget:
        if not state.warned_high:
            await notify_retention(
                cfg,
                title=f"clipd store at {ratio:.0%} of budget",
                body=f"{human_bytes(result.total_bytes)} of {human_bytes(budget)} used.",
            )
            state.warned_high = True
    elif ratio < WARN_THRESHOLD:
        state.warned_high = False

    return result


async def sweep_loop(conn, cfg: Config, state: RetentionState) -> None:
    while True:
        await asyncio.sleep(cfg.sweep_interval_s)
        try:
            await sweep_and_notify(conn, cfg, state)
        except Exception:
            log.exception("retention sweep failed; will retry next interval")
