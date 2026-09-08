"""Entrypoint."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .config import Config
from .daemon import Daemon
from .games import load_games
from .platform import get_adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipwatch")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--games", type=Path, default=Path("games.toml"))
    parser.add_argument("--adapter", default=None,
                        help="force a platform adapter: windows | null")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        cfg = Config.load(args.config, os.environ)
    except (KeyError, ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if not cfg.watch_dir.is_dir():
        print(f"watch_dir does not exist: {cfg.watch_dir}", file=sys.stderr)
        return 2

    daemon = Daemon(cfg, get_adapter(args.adapter), load_games(args.games))
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
