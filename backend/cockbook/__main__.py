"""Einstiegspunkt: ``python -m cockbook [--config PFAD] [--symbol SYMBOL]``.

Startet Datenquellen (Orderbücher bei Bedarf) -> EventBus -> Heatmap-Aggregation
-> WebSocket-Bridge.
Das Frontend (``make dev-frontend``) verbindet sich auf ``ws://127.0.0.1:8765/ws``.

Server-Betrieb (Docker auf einer VM, siehe deploy/): ``COCKBOOK_PUBLIC=1`` lauscht auf
allen Schnittstellen *im Container* – nur zusammen mit ``COCKBOOK_TOKEN`` (mind. 24 Zeichen),
sonst startet das Backend nicht. Nach außen erreichbar ist es nur über Caddy (HTTPS).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from pathlib import Path

from cockbook.app import SourceManager, configured_symbols, install_stop_signals
from cockbook.config import AppConfig, load_config
from cockbook.core.eventbus import EventBus
from cockbook.log import setup_logging
from cockbook.server.bridge import Bridge
from cockbook.server.feed import MarketFeed
from cockbook.trading.live.bitunix_client import Credentials
from cockbook.trading.live.service import LiveAccountService
from cockbook.trading.paper.service import PaperService

log = logging.getLogger("cockbook")


MIN_TOKEN_LEN = 24


def server_mode(cfg: AppConfig) -> tuple[AppConfig, str | None]:
    """Umgebungsvariablen für den Server-Betrieb auswerten. Gibt (Config, Token) zurück."""
    if os.environ.get("COCKBOOK_PUBLIC", "") not in ("1", "true", "yes"):
        return cfg, None
    token = os.environ.get("COCKBOOK_TOKEN", "")
    if len(token) < MIN_TOKEN_LEN:
        raise SystemExit(
            f"COCKBOOK_PUBLIC=1 braucht COCKBOOK_TOKEN mit mindestens {MIN_TOKEN_LEN} Zeichen "
            "(z. B. erzeugen mit: openssl rand -hex 24)"
        )
    # bewusst an der Validierung vorbei: Loopback-Pflicht gilt nur ohne Token
    server = cfg.server.model_copy(update={"host": "0.0.0.0"})
    log.info("server mode: listening on all interfaces, token required")
    return cfg.model_copy(update={"server": server}), token


async def run(cfg: AppConfig, symbol: str | None, token: str | None = None) -> None:
    stop = asyncio.Event()
    install_stop_signals(stop)
    bridge = Bridge(cfg.server, token=token)
    await bridge.start()
    try:
        async with EventBus() as bus:
            manager = SourceManager(cfg, bus, only_symbol=symbol)
            feed = MarketFeed.from_config(cfg, bus, bridge)
            bridge.set_symbol_order(configured_symbols(cfg))
            # Wählt das Frontend einen Coin, startet dessen Orderbuch (falls noch nicht aktiv)
            bridge.on_subscribe = manager.activate
            bridge.long_handler = feed.long_request
            paper = (
                PaperService(cfg.trading.paper, feed.aggs, bus, bridge, cfg.recorder.data_dir)
                if cfg.trading.paper.enabled
                else None
            )
            if paper is not None:
                bridge.paper_handler = paper.handle
            # Echtes Konto (Stufe 1: nur lesen) – Keys nur aus der Umgebung
            live = LiveAccountService(cfg.trading.live, feed.aggs, bridge, Credentials.from_env())
            async with asyncio.TaskGroup() as tg:
                tg.create_task(feed.run(stop))
                tg.create_task(manager.run(stop))
                if paper is not None:
                    tg.create_task(paper.run(stop))
                tg.create_task(live.run(stop))
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
    parser.add_argument("--symbol", default=None, help="Nur dieses Symbol starten (Default: alle)")
    args = parser.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    with contextlib.suppress(KeyboardInterrupt):
        cfg, token = server_mode(cfg)
        asyncio.run(run(cfg, args.symbol, token))
    log.info("backend stopped")


if __name__ == "__main__":
    main()
