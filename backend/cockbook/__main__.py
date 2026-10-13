"""Einstiegspunkt: ``python -m cockbook [--config PFAD] [--symbol SYMBOL]``.

Startet Datenquelle -> EventBus -> Heatmap-Aggregation -> WebSocket-Bridge.
Das Frontend (``make dev-frontend``) verbindet sich auf ``ws://127.0.0.1:8765/ws``.
"""

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
from cockbook.server.bridge import Bridge
from cockbook.server.feed import HeatmapFeed

log = logging.getLogger("cockbook")


async def run(cfg: AppConfig, symbol: str | None) -> None:
    stop = asyncio.Event()
    install_stop_signals(stop)
    bridge = Bridge(cfg.server)
    await bridge.start()
    try:
        async with EventBus() as bus:
            # Phase 2: ein Symbol. Multi-Symbol/Multibook folgt in Phase 5.
            sources = build_sources(cfg, bus, only_symbol=symbol)[:1]
            feed = HeatmapFeed(cfg.aggregation, cfg.ui, bus, bridge)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(feed.run(stop))
                tg.create_task(sources[0].run())
                tg.create_task(stop_sources_on(stop, sources))
    finally:
        await bridge.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="cockbook")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("COCKBOOK_CONFIG", "config.yaml")),
        help="Pfad zur config.yaml (Default: $COCKBOOK_CONFIG oder ./config.yaml)",
    )
    parser.add_argument("--symbol", default=None, help="Default: erstes Symbol der Config")
    args = parser.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(cfg, args.symbol))
    log.info("backend stopped")


if __name__ == "__main__":
    main()
