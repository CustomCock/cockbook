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
from pathlib import Path

from cockbook.config import AggregationConfig, AppConfig, UiConfig
from cockbook.core.aggregation import HeatmapAggregator, HeatmapColumn, LongTermAccumulator
from cockbook.core.eventbus import EventBus, Overflow
from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    Candle,
    CandleBar,
    CandleHistory,
    Event,
    InstrumentInfo,
    Liquidation,
    LongShortRatio,
    MarketSession,
    MarkPrice,
    OpenInterest,
    TakerVolume,
    Ticker24h,
    Trade,
)
from cockbook.server.bridge import Bridge
from cockbook.server.longterm_archive import LongTermArchive, frame_ts
from cockbook.server.protocol import (
    encode_bars,
    encode_column,
    encode_control,
    encode_liquidation,
    encode_live_candle,
    encode_long_column,
    encode_session,
    encode_stat,
    encode_sync,
)

log = logging.getLogger(__name__)

_CHART_EVENTS: tuple[type[Event], ...] = (
    InstrumentInfo,
    BookSnapshot,
    BookDelta,
    BookSyncStatus,
    Trade,
    Liquidation,
    Candle,
    CandleHistory,
)
_MAX_1M_CANDLES = 2000
_STAT_EVENTS: tuple[type[Event], ...] = (
    MarkPrice,
    Ticker24h,
    OpenInterest,
    LongShortRatio,
    TakerVolume,
    MarketSession,
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
        labels: dict[str, str] | None = None,
        archive_dir: Path | None = None,
        long_history_columns: int = 0,
    ) -> None:
        """``symbols``: Symbol -> feste Bucket-Größe in Ticks oder None (automatisch).

        Symbole erscheinen im Frontend erst mit ihrem ``InstrumentInfo`` – ein Coin, den
        es an der Börse nicht (mehr) gibt, taucht so gar nicht erst in der Liste auf.
        """
        self.labels = labels or {}
        self.cfg = agg_cfg
        self.ui = ui_cfg
        self.bus = bus
        self.bridge = bridge
        self.mode = mode
        self.aggs: dict[str, HeatmapAggregator] = {}
        self._hello_sent: set[str] = set()
        self._candles: dict[str, dict[int, CandleBar]] = {}
        self._candle_dirty: dict[str, tuple[CandleBar, bool]] = {}
        self._resyncs: dict[str, int] = {}
        self._long: dict[str, LongTermAccumulator] = {}
        for sym, bt in symbols.items():
            self._add_symbol(sym, bt)
        # Langzeit-Heatmap auf Platte (None = aus)
        self._archive_dir = archive_dir if agg_cfg.long_persist else None
        self._archives: dict[str, LongTermArchive] = {}
        self._long_retention_ms = long_history_columns * agg_cfg.long_interval_ms
        # Messwerte für die Performance-Auswertung
        self.columns = 0
        self.build_ns_total = 0
        self.build_ns_max = 0

    def _add_symbol(self, sym: str, bucket_ticks: int | None = None) -> HeatmapAggregator:
        """Aggregator anlegen – vorab für konfigurierte Symbole, sonst beim ersten
        ``InstrumentInfo`` (automatisch gefundene TradFi-Kontrakte)."""
        agg = self.aggs.get(sym)
        if agg is None:
            agg = self.aggs[sym] = HeatmapAggregator(
                bucket_ticks=bucket_ticks,
                half_range_buckets=self.cfg.half_range_buckets,
                bucket_bps=self.cfg.bucket_bps,
            )
            self._candles[sym] = {}
            self._resyncs[sym] = 0
            # Langzeit: das Buch wird direkt abgetastet (schon grob) -> Faktor 1 hier
            self._long[sym] = LongTermAccumulator(1, pad=self.cfg.long_half_range_buckets)
        return agg

    def _archive(self, exchange: str) -> LongTermArchive | None:
        if self._archive_dir is None or self._long_retention_ms <= 0:
            return None
        a = self._archives.get(exchange)
        if a is None:
            a = self._archives[exchange] = LongTermArchive(
                self._archive_dir, exchange, self._long_retention_ms
            )
        return a

    @classmethod
    def from_config(cls, cfg: AppConfig, bus: EventBus, bridge: Bridge) -> MarketFeed:
        symbols: dict[str, int | None] = {}
        labels: dict[str, str] = {}
        for ex in cfg.exchanges:
            if ex.enabled:
                for s in ex.symbols:
                    symbols[s.symbol.upper()] = s.bucket_ticks
                    if s.label:
                        labels[s.symbol.upper()] = s.label
        return cls(
            symbols,
            cfg.aggregation,
            cfg.ui,
            bus,
            bridge,
            labels=labels,
            archive_dir=cfg.recorder.data_dir,
            long_history_columns=cfg.server.long_history_columns,
        )

    async def _on_chart_event(self, e: Event) -> None:
        if isinstance(e, Liquidation):
            # Gesamtmarkt: Liste aller Coins; Marker nur für eigene Symbole (Aggregator)
            self.bridge.publish_stat(e.symbol, "liq", encode_liquidation(e))
        if isinstance(e, CandleHistory) and e.interval == "1d":
            self.bridge.publish_stat(e.symbol, "d1", encode_bars("d1", e.symbol, list(e.bars)))
            return
        agg = self.aggs.get(e.symbol)
        if agg is None:
            if not isinstance(e, InstrumentInfo):
                return
            agg = self._add_symbol(e.symbol)
        if isinstance(e, CandleHistory):
            store = self._candles[e.symbol]
            for b in e.bars:
                store[b.open_time_ns] = b
            self._publish_candle_history(e.symbol)
            return
        if isinstance(e, Candle):
            if e.interval == "1m":
                self._candles[e.symbol][e.bar.open_time_ns] = e.bar
                self._candle_dirty[e.symbol] = (e.bar, e.closed)
            return
        agg.on_event(e)
        if isinstance(e, InstrumentInfo):
            self.bridge.register_symbol(
                e.symbol,
                exchange=e.exchange,
                tick_size=str(e.tick_size),
                kind=e.kind,
                category=e.category,
                market=e.market or "",
                label=self.labels.get(e.symbol, e.symbol.removesuffix("USDT")),
            )
        elif isinstance(e, BookSyncStatus):
            if not e.synced:
                self._resyncs[e.symbol] += 1
            self.bridge.set_status(
                e.symbol,
                encode_control("status", symbol=e.symbol, synced=e.synced, reason=e.reason),
            )
            self.bridge.publish_stat(
                e.symbol,
                "sync",
                encode_sync(e.symbol, e.ts_ns, e.synced, self._resyncs[e.symbol], e.reason),
            )

    def _publish_candle_history(self, symbol: str) -> None:
        store = self._candles[symbol]
        while len(store) > _MAX_1M_CANDLES:
            del store[min(store)]
        bars = [store[t] for t in sorted(store)]
        self.bridge.set_static(symbol, "kh", encode_bars("kh", symbol, bars))

    def _flush_candles(self) -> None:
        """Laufende Kerzen höchstens einmal pro Spalten-Intervall senden."""
        for sym, (bar, closed) in self._candle_dirty.items():
            self.bridge.send_live(sym, encode_live_candle(sym, bar, closed))
            if closed:
                self._publish_candle_history(sym)  # Historie für neue Abonnenten aktuell halten
        self._candle_dirty.clear()

    async def _on_stat_event(self, e: Event) -> None:
        if isinstance(e, MarketSession):  # gilt für einen ganzen Markt, nicht ein Symbol
            self.bridge.publish_stat(e.symbol, "sess", encode_session(e))
            return
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
                long_interval_ms=self.cfg.long_interval_ms,
                long_bucket_factor=self.cfg.long_bucket_factor,
                long_half_range_buckets=self.cfg.long_half_range_buckets,
                long_history_columns=self.bridge.cfg.long_history_columns,
                half_range_buckets=self.cfg.half_range_buckets,
                ui=self.ui.model_dump(),
            ),
        )
        self._hello_sent.add(symbol)

    async def _load_archive(self, symbol: str, agg: HeatmapAggregator) -> None:
        """Gespeicherte Langzeit-Spalten (gleiche Skalierung) in die Historie laden."""
        assert agg.instrument is not None
        assert agg.bucket_ticks is not None
        archive = self._archive(agg.instrument.exchange)
        if archive is None:
            return
        now_ms = time.time_ns() // 1_000_000
        frames = await asyncio.to_thread(
            archive.load, symbol, now_ms, agg.bucket_ticks, self.cfg.long_bucket_factor
        )
        await asyncio.to_thread(archive.prune, symbol, now_ms)
        for frame in frames:
            self.bridge.publish_long(symbol, frame, frame_ts(frame))
        if frames:
            log.info("longterm history restored", extra={"symbol": symbol, "columns": len(frames)})

    def _publish_long(
        self, sym: str, agg: HeatmapAggregator, col: HeatmapColumn, ts_ms: int
    ) -> None:
        assert agg.bucket_ticks is not None
        assert agg.instrument is not None
        frame = encode_long_column(sym, col, agg.bucket_ticks, self.cfg.long_bucket_factor)
        self.bridge.publish_long(sym, frame, ts_ms)
        archive = self._archive(agg.instrument.exchange)
        if archive is not None and col.synced:
            archive.append(sym, ts_ms, frame)

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
        long_ns = self.cfg.long_interval_ms * 1_000_000
        sample_ns = self.cfg.long_sample_ms * 1_000_000
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
            self._flush_candles()
            for sym, agg in self.aggs.items():
                if not agg.ready:
                    continue
                if sym not in self._hello_sent:
                    self._send_hello(sym, agg)
                    await self._load_archive(sym, agg)
                t0 = time.perf_counter_ns()
                col = agg.make_column(boundary)
                frames = encode_column(sym, col)
                long_acc = self._long[sym]
                if boundary % sample_ns == 0:
                    long_acc.add(
                        agg.make_coarse_column(
                            boundary,
                            self.cfg.long_bucket_factor,
                            self.cfg.long_half_range_buckets,
                        )
                    )
                dt = time.perf_counter_ns() - t0
                ts_ms = boundary // 1_000_000
                self.bridge.publish(sym, frames, ts_ms)
                if boundary % long_ns == 0:
                    self._publish_long(sym, agg, long_acc.take(boundary), ts_ms)
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
