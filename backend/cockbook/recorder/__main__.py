"""``python -m cockbook.recorder [--config PFAD]`` – zeichnet nur auf, sonst nichts."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from pathlib import Path

from cockbook.app import build_sources, install_stop_signals, stop_sources_on
from cockbook.config import AppConfig, load_config
from cockbook.core.eventbus import EventBus
from cockbook.log import setup_logging
from cockbook.recorder.service import Recorder

log = logging.getLogger("cockbook.recorder")


async def run(cfg: AppConfig) -> None:
    stop = asyncio.Event()
    install_stop_signals(stop)
    async with EventBus() as bus:
        recorder = Recorder(cfg.recorder, bus)
        sources = build_sources(cfg, bus, always_on_only=True)
        # Reihenfolge beim Beenden: erst alle Quellen (die noch Abmelde-Metadaten
        # publizieren), danach den Recorder, damit sein letzter Flush alles enthält.
        recorder_stop = asyncio.Event()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(recorder.run(recorder_stop))
            try:
                async with asyncio.TaskGroup() as sources_tg:
                    for src in sources:
                        sources_tg.create_task(src.run())
                    sources_tg.create_task(stop_sources_on(stop, sources))
            finally:
                recorder_stop.set()


def main() -> None:
    parser = argparse.ArgumentParser(prog="cockbook.recorder")
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get("COCKBOOK_CONFIG", "config.yaml"))
    )
    args = parser.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
