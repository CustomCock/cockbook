"""Einstiegspunkt: ``python -m cockbook [--config PFAD]``.

Phase 0: lädt die Config, richtet Logging und EventBus ein und wartet auf Ctrl+C.
Adapter, Aggregation und Server werden in späteren Phasen hier verdrahtet.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from pathlib import Path

from cockbook.config import AppConfig, load_config
from cockbook.core.eventbus import EventBus
from cockbook.log import setup_logging

log = logging.getLogger("cockbook")


async def run(cfg: AppConfig) -> None:
    async with EventBus():
        log.info(
            "backend started (phase 0 skeleton, no adapters yet)",
            extra={"server": f"{cfg.server.host}:{cfg.server.port}"},
        )
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(prog="cockbook")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("COCKBOOK_CONFIG", "config.yaml")),
        help="Pfad zur config.yaml (Default: $COCKBOOK_CONFIG oder ./config.yaml)",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(cfg))
    log.info("backend stopped")


if __name__ == "__main__":
    main()
