"""Gemeinsame Bausteine für die CLI-Einstiegspunkte."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from cockbook.adapters.binance import (
    BinanceLiquidationStream,
    BinanceMarketStats,
    BinanceUsdmAdapter,
)
from cockbook.adapters.binance_history import BinanceDailyHistory
from cockbook.adapters.common import MarketDataSource
from cockbook.config import AppConfig, BinanceUsdmConfig
from cockbook.core.eventbus import EventBus

log = logging.getLogger(__name__)


def configured_symbols(cfg: AppConfig) -> list[str]:
    return [s.symbol.upper() for ex in cfg.exchanges if ex.enabled for s in ex.symbols]


def build_sources(
    cfg: AppConfig,
    bus: EventBus,
    *,
    only_symbol: str | None = None,
    market_wide: bool = True,
    always_on_only: bool = False,
) -> list[MarketDataSource]:
    """Orderbuch-Adapter (alle, nur ``always_on`` oder nur ``only_symbol``); zusätzlich
    (``market_wide``) Quellen für den Gesamtmarkt: Kennzahlen + Liquidationen aller Coins,
    Tageskerzen. Symbol-Adapter stehen immer vorne in der Liste."""
    sources: list[MarketDataSource] = []
    extra: list[MarketDataSource] = []
    for ex in cfg.exchanges:
        if not ex.enabled:
            continue
        for s in ex.symbols:
            if only_symbol is not None:
                if s.symbol.upper() == only_symbol.upper():
                    sources.append(BinanceUsdmAdapter(ex, s.symbol, bus))
            elif s.always_on or not always_on_only:
                sources.append(BinanceUsdmAdapter(ex, s.symbol, bus))
        if market_wide:
            wanted = [s.symbol for s in ex.symbols]
            if only_symbol is not None:
                wanted = [only_symbol.upper()]
            extra.append(BinanceMarketStats(ex, wanted, bus))
            if ex.liquidations_all_market:
                extra.append(BinanceLiquidationStream(ex, bus))
    symbols = configured_symbols(cfg) if only_symbol is None else [only_symbol.upper()]
    if market_wide and cfg.history.enabled and symbols:
        extra.append(BinanceDailyHistory(cfg.history, symbols, cfg.recorder.data_dir, bus))
    if not sources and only_symbol is not None:
        raise SystemExit(f"Symbol {only_symbol} steht nicht in der Config.")
    if not sources and not extra:
        raise SystemExit("Keine aktivierte Börse/kein passendes Symbol in der Config.")
    return sources + extra


class SourceManager:
    """Startet Orderbuch-Adapter bei Bedarf.

    20+ Orderbücher gleichzeitig hieße: 20 Depth-Streams, 20 Snapshots (Gewicht 20 je
    Stück), viel CPU für Coins, die gerade niemand ansieht. Deshalb laufen nur die
    ``always_on``-Symbole ab Start; jedes andere startet, sobald es im Frontend gewählt
    wird, und läuft dann weiter (Zurückschalten zeigt sofort Historie).
    Kennzahlen (Preis, Funding, 24h …) kommen für alle Coins aus Gesamtmarkt-Streams.
    """

    def __init__(self, cfg: AppConfig, bus: EventBus, *, only_symbol: str | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self._only = only_symbol.upper() if only_symbol else None
        self._tg: asyncio.TaskGroup | None = None
        self._stopping = False
        self.sources: list[MarketDataSource] = []
        self.active: set[str] = set()
        self._adapters: dict[str, tuple[MarketDataSource, asyncio.Task[None]]] = {}

    def _exchange_for(self, symbol: str) -> BinanceUsdmConfig | None:
        for ex in self.cfg.exchanges:
            if ex.enabled and any(s.symbol.upper() == symbol for s in ex.symbols):
                return ex
        return None

    def activate(self, symbol: str) -> None:
        """Orderbuch für ``symbol`` starten (idempotent; unbekannte Symbole ignorieren)."""
        symbol = symbol.upper()
        if self._tg is None or self._stopping or symbol in self._adapters:
            return
        if self._only is not None and symbol != self._only:
            return
        ex = self._exchange_for(symbol)
        if ex is None:
            return
        adapter = BinanceUsdmAdapter(ex, symbol, self.bus)
        task = self._tg.create_task(self._guard(adapter), name=f"source:{symbol}")
        self._adapters[symbol] = (adapter, task)
        self.active.add(symbol)
        log.info("orderbook activated", extra={"symbol": symbol})

    async def _guard(self, src: MarketDataSource) -> None:
        """Ein Adapter, der dauerhaft scheitert (z. B. Symbol delistet), darf weder die
        anderen Coins noch das Backend mitreißen. Nach einem Fehler darf er per erneutem
        Abo wieder gestartet werden."""
        try:
            await src.run()
        except Exception:
            log.exception("source failed", extra={"source": src.name})
            sym = src.name.split(":")[-1]
            self._adapters.pop(sym, None)
            self.active.discard(sym)

    def _initial_symbols(self) -> list[str]:
        if self._only is not None:
            return [self._only]
        return [
            s.symbol.upper() for ex in self.cfg.exchanges if ex.enabled for s in ex.symbols
            if s.always_on
        ]  # fmt: skip

    def _market_sources(self) -> list[MarketDataSource]:
        """Gesamtmarkt-Quellen (Kennzahlen, Liquidationen, Tageskerzen) – keine Orderbücher."""
        all_sources = build_sources(self.cfg, self.bus, only_symbol=self._only, always_on_only=True)
        return [s for s in all_sources if not isinstance(s, BinanceUsdmAdapter)]

    async def run(self, stop: asyncio.Event) -> None:
        self.sources = self._market_sources()
        async with asyncio.TaskGroup() as tg:
            self._tg = tg
            for src in self.sources:
                tg.create_task(self._guard(src), name=f"source:{src.name}")
            for sym in self._initial_symbols():
                self.activate(sym)
            await stop.wait()
            self._stopping = True
            for src in self.sources:
                src.stop()
            for adapter, _ in list(self._adapters.values()):
                adapter.stop()
        self._tg = None


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
