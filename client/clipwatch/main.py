"""Entrypoint."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from logging.handlers import RotatingFileHandler

from .config import Config
from .daemon import Daemon
from .games import load_games
from .platform import get_adapter


def _bundled(name: str) -> Path:
    """Locate a data file next to the exe, or inside the PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        for base in (Path(sys.executable).parent, Path(getattr(sys, "_MEIPASS", "."))):
            candidate = base / name
            if candidate.exists():
                return candidate
    return Path(name)


def _add_file_log(cfg: Config, level: str) -> None:
    """A windowed build has no console, so without this there is no record at all."""
    try:
        cfg.queue_dir.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            cfg.queue_dir.parent / "clipwatch.log",
            maxBytes=2_000_000, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(level)
    except OSError:
        pass  # console logging still applies


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipwatch")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--games", type=Path, default=None)
    parser.add_argument("--adapter", default=None,
                        help="force a platform adapter: windows | null")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config_path = args.config or _bundled("config.toml")
    games_path = args.games or _bundled("games.toml")

    try:
        cfg = Config.load(config_path, os.environ)
    except (KeyError, ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if not cfg.watch_dir.is_dir():
        print(f"watch_dir does not exist: {cfg.watch_dir}", file=sys.stderr)
        return 2

    _add_file_log(cfg, args.log_level)

    mapping = load_games(games_path)
    if not mapping:
        logging.warning("no game mappings loaded from %s; games will resolve by "
                        "window title or Unknown", games_path)

    daemon = Daemon(cfg, get_adapter(args.adapter), mapping)
    daemon.start()

    signal.signal(signal.SIGINT, lambda *_: daemon.stop())
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop())

    try:
        while not daemon._stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
