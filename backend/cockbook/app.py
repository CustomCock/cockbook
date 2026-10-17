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
from cockbook.adapters.bitunix import BitunixAdapter, BitunixMarketStats
from cockbook.adapters.common import MarketDataSource
from cockbook.config import AppConfig, BinanceUsdmConfig, BitunixConfig
from cockbook.core.eventbus import EventBus
from cockbook.core.venue import COMBINED, DEFAULT_EXCHANGE, split_key, venue_key

log = logging.getLogger(__name__)


def _binance(cfg: AppConfig) -> list[BinanceUsdmConfig]:
    return [ex for ex in cfg.exchanges if ex.enabled and isinstance(ex, BinanceUsdmConfig)]


def _bitunix(cfg: AppConfig) -> list[BitunixConfig]:
    return [ex for ex in cfg.exchanges if ex.enabled and isinstance(ex, BitunixConfig)]


def configured_symbols(cfg: AppConfig) -> list[str]:
    """Reihenfolge im Coin-Menü: Binance-Coins aus der Config, dann feste Bitunix-Coins."""
    keys = [s.symbol.upper() for ex in _binance(cfg) for s in ex.symbols]
    keys += [venue_key("bitunix", s.symbol.upper()) for ex in _bitunix(cfg) for s in ex.symbols]
    return keys


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
    only = split_key(only_symbol.upper()) if only_symbol else None
    for ex in _binance(cfg):
        for s in ex.symbols:
            if only is not None:
                if only == (DEFAULT_EXCHANGE, s.symbol.upper()):
                    sources.append(BinanceUsdmAdapter(ex, s.symbol, bus))
            elif s.always_on or not always_on_only:
                sources.append(BinanceUsdmAdapter(ex, s.symbol, bus))
        if market_wide:
            wanted = [s.symbol for s in ex.symbols]
            if only is not None:
                wanted = [only[1]]
            extra.append(BinanceMarketStats(ex, wanted, bus, discover=only is None))
            if ex.liquidations_all_market:
                extra.append(BinanceLiquidationStream(ex, bus))
    for bx in _bitunix(cfg):
        for s in bx.symbols:
            if only is not None:
                if only == ("bitunix", s.symbol.upper()):
                    sources.append(BitunixAdapter(bx, s.symbol, bus))
            elif s.always_on or not always_on_only:
                sources.append(BitunixAdapter(bx, s.symbol, bus))
        if market_wide and only is None:
            cats = {k: v for ex in _binance(cfg) for k, v in ex.tradfi_categories.items()}
            mirrored = [s.symbol.upper() for ex in _binance(cfg) for s in ex.symbols]
            mirrored += [s.symbol.upper() for s in bx.symbols]
            extra.append(BitunixMarketStats(bx, None if bx.all_symbols else mirrored, bus, cats))
    # Tageskerzen (ATH, Zyklen) nur einmal je Coin – von Binance
    symbols = [s.symbol.upper() for ex in _binance(cfg) for s in ex.symbols]
    if only is not None:
        symbols = [only[1]]
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

    def _adapter_for(self, key: str) -> MarketDataSource | None:
        """Passenden Adapter für Coin+Börse bauen; None = unbekannt/nicht handelbar."""
        exchange, sym = split_key(key)
        if exchange == DEFAULT_EXCHANGE:
            for ex in _binance(self.cfg):
                if any(s.symbol.upper() == sym for s in ex.symbols):
                    return BinanceUsdmAdapter(ex, sym, self.bus)
            # automatisch gefundene TradFi-Kontrakte
            for src in self.sources:
                if isinstance(src, BinanceMarketStats) and sym in src.available:
                    return BinanceUsdmAdapter(src.cfg, sym, self.bus)
        elif exchange == "bitunix":
            for bx in _bitunix(self.cfg):
                if any(s.symbol.upper() == sym for s in bx.symbols):
                    return BitunixAdapter(bx, sym, self.bus)
            for src in self.sources:
                if isinstance(src, BitunixMarketStats) and sym in src.available:
                    return BitunixAdapter(src.cfg, sym, self.bus)
        return None

    def activate(self, key: str) -> None:
        """Orderbuch für Coin+Börse starten (idempotent; unbekannte ignorieren).
        ``MULTI:BTCUSDT`` startet die Bücher aller Börsen dieses Coins."""
        key = key.upper()
        exchange, sym = split_key(key)
        if exchange == COMBINED:
            for k in (sym, venue_key("bitunix", sym)):
                self.activate(k)
            return
        if self._tg is None or self._stopping or key in self._adapters:
            return
        if self._only is not None and key != self._only:
            return
        adapter = self._adapter_for(key)
        if adapter is None:
            return
        task = self._tg.create_task(self._guard(adapter, key), name=f"source:{key}")
        self._adapters[key] = (adapter, task)
        self.active.add(key)
        log.info("orderbook activated", extra={"symbol": key})

    async def _guard(self, src: MarketDataSource, key: str | None = None) -> None:
        """Ein Adapter, der dauerhaft scheitert (z. B. Symbol delistet), darf weder die
        anderen Coins noch das Backend mitreißen. Nach einem Fehler darf er per erneutem
        Abo wieder gestartet werden."""
        try:
            await src.run()
        except Exception:
            log.exception("source failed", extra={"source": src.name})
            if key is not None:
                self._adapters.pop(key, None)
                self.active.discard(key)

    def _initial_symbols(self) -> list[str]:
        if self._only is not None:
            return [self._only]
        keys = [s.symbol.upper() for ex in _binance(self.cfg) for s in ex.symbols if s.always_on]
        keys += [
            venue_key("bitunix", s.symbol.upper()) for bx in _bitunix(self.cfg) for s in bx.symbols
            if s.always_on
        ]  # fmt: skip
        return keys

    def _market_sources(self) -> list[MarketDataSource]:
        """Gesamtmarkt-Quellen (Kennzahlen, Liquidationen, Tageskerzen) – keine Orderbücher."""
        all_sources = build_sources(self.cfg, self.bus, only_symbol=self._only, always_on_only=True)
        return [s for s in all_sources if not isinstance(s, BinanceUsdmAdapter | BitunixAdapter)]

    async def _history_for_discovered(self, stop: asyncio.Event) -> None:
        """Tageskerzen auch für automatisch gefundene TradFi-Kontrakte laden."""
        stats = [s for s in self.sources if isinstance(s, BinanceMarketStats)]
        history = [s for s in self.sources if isinstance(s, BinanceDailyHistory)]
        if not stats or not history:
            return
        for st in stats:
            ready = asyncio.ensure_future(st.ready.wait())
            stopped = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({ready, stopped}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                ready.cancel()
                stopped.cancel()
            if stop.is_set():
                return
            for h in history:
                h.add_symbols(st.discovered, futures_only=True)

    async def run(self, stop: asyncio.Event) -> None:
        self.sources = self._market_sources()
        async with asyncio.TaskGroup() as tg:
            self._tg = tg
            for src in self.sources:
                tg.create_task(self._guard(src), name=f"source:{src.name}")
            tg.create_task(self._history_for_discovered(stop))
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
