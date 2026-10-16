"""Gemeinsame Bausteine für die CLI-Einstiegspunkte."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from cockbook.adapters.binance import BinanceLiquidationStream, BinanceUsdmAdapter
from cockbook.adapters.binance_history import BinanceDailyHistory
from cockbook.adapters.common import MarketDataSource
from cockbook.config import AppConfig
from cockbook.core.eventbus import EventBus


def build_sources(
    cfg: AppConfig, bus: EventBus, *, only_symbol: str | None = None, market_wide: bool = True
) -> list[MarketDataSource]:
    """Ein Adapter pro Symbol; zusätzlich (``market_wide``) Quellen für den Gesamtmarkt,
    z. B. Liquidationen aller Coins. Symbol-Adapter stehen immer vorne in der Liste."""
    sources: list[MarketDataSource] = []
    extra: list[MarketDataSource] = []
    for ex in cfg.exchanges:
        if not ex.enabled:
            continue
        for s in ex.symbols:
            if only_symbol is None or s.symbol.upper() == only_symbol.upper():
                sources.append(BinanceUsdmAdapter(ex, s.symbol, bus))
        if market_wide and ex.liquidations_all_market:
            extra.append(BinanceLiquidationStream(ex, bus))
    if market_wide and cfg.history.enabled and sources:
        symbols = [s.name.split(":")[-1] for s in sources]
        extra.append(BinanceDailyHistory(cfg.history, symbols, cfg.recorder.data_dir, bus))
    if not sources:
        raise SystemExit("Keine aktivierte Börse/kein passendes Symbol in der Config.")
    return sources + extra


def install_stop_signals(stop: asyncio.Event) -> None:
    """Geordnetes Herunterfahren bei SIGINT (Ctrl+C) und SIGTERM (systemd).

    Erstes Signal setzt ``stop`` (Quellen beenden sich, Recorder flusht zuletzt),
    ein zweites bricht hart ab. Unter Windows gibt es ``add_signal_handler`` nicht;
    dort bricht ``asyncio.run`` bei Ctrl+C den Haupt-Task ab – gepufferte Daten
    werden trotzdem in den ``finally``-Blöcken geschrieben.
    """
    loop = asyncio.get_running_loop()
    main = asyncio.current_task()

    def on_signal() -> None:
        if stop.is_set() and main is not None:
            main.cancel()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, on_signal)


async def stop_sources_on(stop: asyncio.Event, sources: list[MarketDataSource]) -> None:
    await stop.wait()
    for s in sources:
        s.stop()
