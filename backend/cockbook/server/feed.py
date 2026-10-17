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

import msgpack

from cockbook.adapters.binance import BinanceKlines
from cockbook.config import (
    AggregationConfig,
    AppConfig,
    BinanceUsdmConfig,
    LongTierConfig,
    UiConfig,
)
from cockbook.core.aggregation import ColumnDeltaEncoder, HeatmapAggregator, LongTermAccumulator
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
from cockbook.core.venue import split_key, venue_key
from cockbook.server.bridge import Bridge
from cockbook.server.longterm_archive import LongTermArchive
from cockbook.server.protocol import (
    encode_bars,
    encode_column,
    encode_control,
    encode_klines,
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
        klines: BinanceKlines | None = None,
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
        self.cfg = agg_cfg
        self.aggs: dict[str, HeatmapAggregator] = {}
        self._encoders: dict[str, ColumnDeltaEncoder] = {}
        self._hello_sent: set[str] = set()
        self._candles: dict[str, dict[int, CandleBar]] = {}
        self._candle_dirty: dict[str, tuple[CandleBar, bool]] = {}
        self._resyncs: dict[str, int] = {}
        self._long: dict[str, dict[str, LongTermAccumulator]] = {}
        for sym, bt in symbols.items():
            self._add_symbol(sym, bt)
        # Langzeit-Heatmap: Stufen, Archiv auf Platte (None = aus), Kerzen auf Anfrage
        self.tiers: dict[str, LongTierConfig] = {t.name: t for t in agg_cfg.long_tiers}
        self._archive_dir = archive_dir if agg_cfg.long_persist else None
        self._archives: dict[tuple[str, str], LongTermArchive] = {}
        self.klines = klines
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
                memory_max_age_s=self.cfg.memory_max_age_s,
            )
            self._encoders[sym] = ColumnDeltaEncoder(self.cfg.keyframe_every_columns)
            self._candles[sym] = {}
            self._resyncs[sym] = 0
            self._long[sym] = {t.name: LongTermAccumulator(t.factor) for t in self.cfg.long_tiers}
        return agg

    def _archive(self, exchange: str, tier: LongTierConfig) -> LongTermArchive | None:
        if self._archive_dir is None:
            return None
        a = self._archives.get((exchange, tier.name))
        if a is None:
            # etwas länger aufheben als angezeigt (Rand beim Scrollen)
            retention = int(tier.span_h * 3_600_000 * 1.05)
            a = self._archives[(exchange, tier.name)] = LongTermArchive(
                self._archive_dir, exchange, tier.name, retention
            )
        return a

    @classmethod
    def from_config(cls, cfg: AppConfig, bus: EventBus, bridge: Bridge) -> MarketFeed:
        symbols: dict[str, int | None] = {}
        labels: dict[str, str] = {}
        binance = [ex for ex in cfg.exchanges if ex.enabled and isinstance(ex, BinanceUsdmConfig)]
        for ex in cfg.exchanges:
            if ex.enabled:
                for s in ex.symbols:
                    key = venue_key(ex.name, s.symbol.upper())
                    symbols[key] = s.bucket_ticks
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
            klines=BinanceKlines(binance[0]) if binance else None,
        )

    async def _on_chart_event(self, e: Event) -> None:
        k = venue_key(e.exchange, e.symbol)  # Coin + Börse
        if isinstance(e, Liquidation):
            # Gesamtmarkt: Liste aller Coins; Marker nur für eigene Symbole (Aggregator)
            self.bridge.publish_stat(k, "liq", encode_liquidation(e))
            for acc in self._long.get(k, {}).values():
                acc.add_liquidation(e.side, float(e.qty * e.price))
        if isinstance(e, CandleHistory) and e.interval == "1d":
            self.bridge.publish_stat(k, "d1", encode_bars("d1", k, list(e.bars)))
            return
        agg = self.aggs.get(k)
        if agg is None:
            if not isinstance(e, InstrumentInfo):
                return
            agg = self._add_symbol(k)
        if isinstance(e, CandleHistory):
            store = self._candles[k]
            for b in e.bars:
                store[b.open_time_ns] = b
            self._publish_candle_history(k)
            return
        if isinstance(e, Candle):
            if e.interval == "1m":
                self._candles[k][e.bar.open_time_ns] = e.bar
                self._candle_dirty[k] = (e.bar, e.closed)
            return
        agg.on_event(e)
        if isinstance(e, InstrumentInfo):
            self.bridge.register_symbol(
                k,
                exchange=e.exchange,
                base=e.symbol,
                tick_size=str(e.tick_size),
                kind=e.kind,
                category=e.category,
                market=e.market or "",
                label=self.labels.get(e.symbol, e.symbol.removesuffix("USDT")),
            )
        elif isinstance(e, BookSyncStatus):
            if not e.synced:
                self._resyncs[k] += 1
            self.bridge.set_status(
                k,
                encode_control("status", symbol=k, synced=e.synced, reason=e.reason),
            )
            self.bridge.publish_stat(
                k,
                "sync",
                encode_sync(k, e.ts_ns, e.synced, self._resyncs[k], e.reason),
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
        k = venue_key(e.exchange, e.symbol)
        if k not in self.aggs:
            return
        encoded = encode_stat(e, k)
        if encoded is not None:
            kind, frame = encoded
            self.bridge.publish_stat(k, kind, frame)

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
                long_tiers=[t.name for t in self.cfg.long_tiers],
                half_range_buckets=self.cfg.half_range_buckets,
                ui=self.ui.model_dump(),
            ),
        )
        self._hello_sent.add(symbol)
        # neue Skalierung -> Historie ist leer, die nächste Spalte muss ein Schlüsselbild sein
        self._encoders[symbol] = ColumnDeltaEncoder(self.cfg.keyframe_every_columns)

    def _publish_tier(
        self, sym: str, agg: HeatmapAggregator, tier: LongTierConfig, ts_ns: int
    ) -> None:
        acc = self._long[sym][tier.name]
        col = acc.take(ts_ns)
        ll, lsh = acc.take_liquidations()
        if agg.bucket_ticks is None or agg.instrument is None:
            return
        frame = encode_long_column(sym, tier.name, col, agg.bucket_ticks, tier.factor, ll, lsh)
        self.bridge.publish_tier(sym, tier.name, frame)
        archive = self._archive(agg.instrument.exchange, tier)
        if archive is not None and (col.synced or ll or lsh):
            # Pfad: <börse>/<reines Symbol> (kein ":" – unter Windows ungültig)
            archive.append(agg.instrument.symbol, ts_ns // 1_000_000, frame)

    async def long_request(self, symbol: str, tier_name: str, what: str) -> list[str | bytes]:
        """Daten für die Langzeit-Ansicht: ``what="all"`` = hello + gespeicherte Spalten +
        Kerzen + ready; ``"klines"`` = nur Kerzen (minütliche Aktualisierung)."""
        tier = self.tiers.get(tier_name)
        if tier is None:
            return [encode_control("error", message=f"unbekannte Stufe {tier_name}")]
        span_ms = int(tier.span_h * 3_600_000)
        out: list[str | bytes] = []
        if what == "all":
            agg = self.aggs.get(symbol)
            inst = agg.instrument if agg is not None else None
            bt = agg.bucket_ticks if agg is not None else None
            exchange, raw = split_key(symbol)
            frames: list[bytes] = []
            archive = self._archive(exchange, tier)
            if archive is not None:
                now_ms = time.time_ns() // 1_000_000
                frames = await asyncio.to_thread(archive.load, raw, now_ms, bt, tier.factor)
                await asyncio.to_thread(archive.prune, raw, now_ms)
                if bt is None and frames:
                    bt = int(msgpack.unpackb(frames[-1])["bt"])
            out.append(
                encode_control(
                    "long_hello",
                    symbol=symbol,
                    tier=tier.name,
                    interval_ms=tier.interval_s * 1000,
                    span_ms=span_ms,
                    factor=tier.factor,
                    bucket_ticks=bt,
                    tick_size=str(inst.tick_size) if inst is not None else None,
                    columns=len(frames),
                    candle_interval=tier.candle_interval,
                    tiers=[t.name for t in self.cfg.long_tiers],
                )
            )
            out.extend(frames)
        if self.klines is not None:
            minutes = _interval_minutes(tier.candle_interval)
            limit = min(1500, int(span_ms / 60_000 / minutes) + 2) if minutes else 500
            # Kerzen kommen von Binance (Preise der Börsen liegen dicht beieinander)
            bars = await self.klines.get(split_key(symbol)[1], tier.candle_interval, limit)
            out.append(encode_klines(symbol, tier.name, tier.candle_interval, bars))
        if what == "all":
            out.append(encode_control("long_ready", symbol=symbol, tier=tier.name))
        return out

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
        tier_ns = [
            (t, t.sample_s * 1_000_000_000, t.interval_s * 1_000_000_000)
            for t in self.cfg.long_tiers
        ]
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
                t0 = time.perf_counter_ns()
                col = self._encoders[sym].encode(agg.make_column(boundary))
                frames = encode_column(sym, col)
                dt = time.perf_counter_ns() - t0
                self.bridge.publish(sym, frames, boundary // 1_000_000, keyframe=col.keyframe)
                # Langzeit-Stufen: Buch abtasten, am Intervallende Spalte abschließen
                bb, ba = col.best_bid, col.best_ask
                for tier, s_ns, i_ns in tier_ns:
                    if boundary % s_ns == 0:
                        self._long[sym][tier.name].add_sample(
                            agg.coarse_sums(tier.factor, self.cfg.long_half_range_buckets), bb, ba
                        )
                    if boundary % i_ns == 0:
                        self._publish_tier(sym, agg, tier, boundary)
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
        if self.klines is not None:
            await self.klines.close()


def _interval_minutes(iv: str) -> int:
    """Binance-Intervall ("5m", "1h", "4h", "1d") in Minuten; 0 = unbekannt."""
    units = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    try:
        return int(iv[:-1]) * units[iv[-1]]
    except (KeyError, ValueError):
        return 0
