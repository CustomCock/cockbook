"""Verbindet Bus -> Aggregatoren (ein Aggregator pro Symbol) -> Bridge.

Ein zentraler Feed statt eines Feeds pro Symbol: jedes Event wird genau einmal
angefasst und per Symbol an den passenden Aggregator verteilt. Spalten werden für
*alle* Symbole gebaut, auch für nicht angezeigte – beim Umschalten im Frontend ist die
Historie dann sofort da.
"""

from __future__ import annotations

import asyncio
import logging
import time

from cockbook.config import AggregationConfig, AppConfig, UiConfig
from cockbook.core.aggregation import HeatmapAggregator
from cockbook.core.eventbus import EventBus, Overflow
from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    Event,
    InstrumentInfo,
    Liquidation,
    LongShortRatio,
    MarkPrice,
    OpenInterest,
    TakerVolume,
    Ticker24h,
    Trade,
)
from cockbook.server.bridge import Bridge
from cockbook.server.protocol import (
    encode_column,
    encode_control,
    encode_liquidation,
    encode_stat,
)

log = logging.getLogger(__name__)

_CHART_EVENTS: tuple[type[Event], ...] = (
    InstrumentInfo,
    BookSnapshot,
    BookDelta,
    BookSyncStatus,
    Trade,
    Liquidation,
)
_STAT_EVENTS: tuple[type[Event], ...] = (
    MarkPrice,
    Ticker24h,
    OpenInterest,
    LongShortRatio,
    TakerVolume,
)


class MarketFeed:
    def __init__(
        self,
        symbols: dict[str, int | None],
        agg_cfg: AggregationConfig,
        ui_cfg: UiConfig,
        bus: EventBus,
        bridge: Bridge,
        *,
        mode: str = "live",
    ) -> None:
        """``symbols``: Symbol -> feste Bucket-Größe in Ticks oder None (automatisch)."""
        self.cfg = agg_cfg
        self.ui = ui_cfg
        self.bus = bus
        self.bridge = bridge
        self.mode = mode
        self.aggs = {
            sym: HeatmapAggregator(
                bucket_ticks=bt,
                half_range_buckets=agg_cfg.half_range_buckets,
                bucket_bps=agg_cfg.bucket_bps,
            )
            for sym, bt in symbols.items()
        }
        self._hello_sent: set[str] = set()
        for sym in symbols:
            bridge.register_symbol(sym)
        # Messwerte für die Performance-Auswertung
        self.columns = 0
        self.build_ns_total = 0
        self.build_ns_max = 0

    @classmethod
    def from_config(cls, cfg: AppConfig, bus: EventBus, bridge: Bridge) -> MarketFeed:
        symbols: dict[str, int | None] = {}
        for ex in cfg.exchanges:
            if ex.enabled:
                for s in ex.symbols:
                    symbols[s.symbol.upper()] = s.bucket_ticks
        return cls(symbols, cfg.aggregation, cfg.ui, bus, bridge)

    async def _on_chart_event(self, e: Event) -> None:
        agg = self.aggs.get(e.symbol)
        if agg is None:
            return
        agg.on_event(e)
        if isinstance(e, InstrumentInfo):
            self.bridge.register_symbol(e.symbol, exchange=e.exchange, tick_size=str(e.tick_size))
        elif isinstance(e, BookSyncStatus):
            self.bridge.set_status(
                e.symbol,
                encode_control("status", symbol=e.symbol, synced=e.synced, reason=e.reason),
            )
        elif isinstance(e, Liquidation) and agg.instrument is not None:
            price = float(e.price_ticks * agg.instrument.tick_size)
            self.bridge.publish_stat(e.symbol, "liq", encode_liquidation(e, price))

    async def _on_stat_event(self, e: Event) -> None:
        if e.symbol not in self.aggs:
            return
        encoded = encode_stat(e)
        if encoded is not None:
            kind, frame = encoded
            self.bridge.publish_stat(e.symbol, kind, frame)

    def _send_hello(self, symbol: str, agg: HeatmapAggregator) -> None:
        assert agg.instrument is not None
        self.bridge.set_hello(
            symbol,
            encode_control(
                "hello",
                mode=self.mode,
                exchange=agg.instrument.exchange,
                symbol=symbol,
                tick_size=str(agg.instrument.tick_size),
                bucket_ticks=agg.bucket_ticks,
                interval_ms=self.cfg.interval_ms,
                half_range_buckets=self.cfg.half_range_buckets,
                ui=self.ui.model_dump(),
            ),
        )
        self._hello_sent.add(symbol)

    async def run(self, stop: asyncio.Event) -> None:
        # Orderbuch-Events dürfen nie verloren gehen -> BLOCK.
        for t in _CHART_EVENTS:
            self.bus.subscribe(
                t, self._on_chart_event, name=f"feed:{t.__name__}", overflow=Overflow.BLOCK
            )
        # Kennzahlen: nur der aktuelle Wert zählt -> verwerfen statt bremsen.
        for t in _STAT_EVENTS:
            self.bus.subscribe(
                t,
                self._on_stat_event,
                name=f"feed:{t.__name__}",
                maxsize=1000,
                overflow=Overflow.DROP_OLDEST,
            )
        interval_ns = self.cfg.interval_ms * 1_000_000
        last_log = time.monotonic()
        while not stop.is_set():
            # Auf Vielfache des Intervalls ausrichten: Spalten-Zeitstempel sind dann über
            # Neustarts und Symbole hinweg konsistent (wichtig für Replay in Phase 4).
            now = time.time_ns()
            boundary = (now // interval_ns + 1) * interval_ns
            try:
                async with asyncio.timeout((boundary - now) / 1e9):
                    await stop.wait()
                break
            except TimeoutError:
                pass
            for sym, agg in self.aggs.items():
                if not agg.ready:
                    continue
                if sym not in self._hello_sent:
                    self._send_hello(sym, agg)
                t0 = time.perf_counter_ns()
                frames = encode_column(sym, agg.make_column(boundary))
                dt = time.perf_counter_ns() - t0
                self.bridge.publish(sym, frames)
                self.columns += 1
                self.build_ns_total += dt
                self.build_ns_max = max(self.build_ns_max, dt)
            if time.monotonic() - last_log > 60 and self.columns:
                last_log = time.monotonic()
                log.info(
                    "heatmap feed",
                    extra={
                        "symbols": len(self.aggs),
                        "columns": self.columns,
                        "build_avg_ms": round(self.build_ns_total / self.columns / 1e6, 3),
                        "build_max_ms": round(self.build_ns_max / 1e6, 3),
                        "clients": self.bridge.client_count,
                    },
                )
