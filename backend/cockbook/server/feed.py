"""Verbindet Bus -> Aggregator -> Bridge und erzeugt Spalten im festen Takt."""

from __future__ import annotations

import asyncio
import logging
import time

from cockbook.config import AggregationConfig, UiConfig
from cockbook.core.aggregation import HeatmapAggregator
from cockbook.core.eventbus import EventBus, Overflow
from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    Event,
    InstrumentInfo,
    Trade,
)
from cockbook.server.bridge import Bridge
from cockbook.server.protocol import encode_column, encode_control

log = logging.getLogger(__name__)


class HeatmapFeed:
    def __init__(
        self,
        agg_cfg: AggregationConfig,
        ui_cfg: UiConfig,
        bus: EventBus,
        bridge: Bridge,
        *,
        mode: str = "live",
    ) -> None:
        self.cfg = agg_cfg
        self.ui = ui_cfg
        self.bus = bus
        self.bridge = bridge
        self.mode = mode
        self.agg = HeatmapAggregator(
            bucket_ticks=agg_cfg.bucket_ticks, half_range_buckets=agg_cfg.half_range_buckets
        )
        # Messwerte für die Performance-Auswertung
        self.columns = 0
        self.build_ns_total = 0
        self.build_ns_max = 0

    async def _on_event(self, e: Event) -> None:
        self.agg.on_event(e)
        if isinstance(e, InstrumentInfo):
            self.bridge.set_hello(
                encode_control(
                    "hello",
                    mode=self.mode,
                    exchange=e.exchange,
                    symbol=e.symbol,
                    tick_size=str(e.tick_size),
                    bucket_ticks=self.cfg.bucket_ticks,
                    interval_ms=self.cfg.interval_ms,
                    half_range_buckets=self.cfg.half_range_buckets,
                    ui=self.ui.model_dump(),
                )
            )
        elif isinstance(e, BookSyncStatus):
            self.bridge.set_status(encode_control("status", synced=e.synced, reason=e.reason))

    async def run(self, stop: asyncio.Event) -> None:
        # Orderbuch-Events dürfen nie verloren gehen -> BLOCK.
        for t in (InstrumentInfo, BookSnapshot, BookDelta, BookSyncStatus, Trade):
            self.bus.subscribe(
                t, self._on_event, name=f"heatmap:{t.__name__}", overflow=Overflow.BLOCK
            )
        interval_ns = self.cfg.interval_ms * 1_000_000
        last_log = time.monotonic()
        while not stop.is_set():
            # Auf Vielfache des Intervalls ausrichten: Spalten-Zeitstempel sind dann über
            # Neustarts hinweg konsistent (wichtig für Replay-Vergleich in Phase 4).
            now = time.time_ns()
            boundary = (now // interval_ns + 1) * interval_ns
            try:
                async with asyncio.timeout((boundary - now) / 1e9):
                    await stop.wait()
                break
            except TimeoutError:
                pass
            if self.agg.instrument is None:
                continue
            t0 = time.perf_counter_ns()
            frames = encode_column(self.agg.make_column(boundary))
            dt = time.perf_counter_ns() - t0
            self.bridge.publish(frames)
            self.columns += 1
            self.build_ns_total += dt
            self.build_ns_max = max(self.build_ns_max, dt)
            if time.monotonic() - last_log > 60:
                last_log = time.monotonic()
                log.info(
                    "heatmap feed",
                    extra={
                        "columns": self.columns,
                        "build_avg_ms": round(self.build_ns_total / self.columns / 1e6, 3),
                        "build_max_ms": round(self.build_ns_max / 1e6, 3),
                        "clients": self.bridge.client_count,
                    },
                )
