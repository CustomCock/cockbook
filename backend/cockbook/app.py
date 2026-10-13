"""Gemeinsame Bausteine für die CLI-Einstiegspunkte."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from cockbook.adapters.binance import BinanceUsdmAdapter
from cockbook.config import AppConfig
from cockbook.core.eventbus import EventBus


def build_sources(
    cfg: AppConfig, bus: EventBus, *, only_symbol: str | None = None
) -> list[BinanceUsdmAdapter]:
    sources = []
    for ex in cfg.exchanges:
        if not ex.enabled:
            continue
        for s in ex.symbols:
            if only_symbol is None or s.symbol.upper() == only_symbol.upper():
                sources.append(BinanceUsdmAdapter(ex, s.symbol, bus))
    if not sources:
        raise SystemExit("Keine aktivierte Börse/kein passendes Symbol in der Config.")
    return sources


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


async def stop_sources_on(stop: asyncio.Event, sources: list[BinanceUsdmAdapter]) -> None:
    await stop.wait()
    for s in sources:
        s.stop()
