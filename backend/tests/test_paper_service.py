"""Paper-Dienst: Anfragen aus dem Browser, Buch aus dem Aggregator, Speichern."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from cockbook.config import PaperConfig, ServerConfig
from cockbook.core.aggregation import HeatmapAggregator
from cockbook.core.eventbus import EventBus
from cockbook.core.events import BookSnapshot, BookSyncStatus, InstrumentInfo, PriceLevel
from cockbook.server.bridge import Bridge
from cockbook.trading.paper.service import PaperService


def agg() -> HeatmapAggregator:
    a = HeatmapAggregator(bucket_ticks=1, half_range_buckets=100)
    base: dict[str, Any] = {"exchange": "bitunix", "symbol": "BTCUSDT", "ts_recv_ns": 1}
    a.on_event(InstrumentInfo(**base, tick_size=Decimal("0.1"), step_size=Decimal("0.001")))
    a.on_event(
        BookSnapshot(
            **base,
            bids=(PriceLevel(999, Decimal(2)), PriceLevel(998, Decimal(5))),
            asks=(PriceLevel(1000, Decimal(1)), PriceLevel(1001, Decimal(3))),
            last_seq=1,
        )
    )
    a.on_event(BookSyncStatus(**base, synced=True, reason=""))
    return a


async def test_service_orders_state_and_persistence(tmp_path: Path) -> None:
    bridge = Bridge(ServerConfig())
    async with EventBus() as bus:
        svc = PaperService(PaperConfig(), {"BITUNIX:BTCUSDT": agg()}, bus, bridge, tmp_path)
        r = svc.handle({"type": "paper", "op": "order", "req": 7, "symbol": "BITUNIX:BTCUSDT",
                        "side": "buy", "kind": "market", "qty": "1.5"})  # fmt: skip
        assert r == {"type": "paper_result", "req": 7, "ok": True,
                     "message": "Market-Kauf 1.5 ausgeführt"}  # fmt: skip
        pos = svc.engine.acc.positions["BITUNIX:BTCUSDT"]
        # 1 @ 100.0 + 0.5 @ 100.1
        assert pos.entry == Decimal("100.033")
        state = json.loads(bridge._shared["paper"])
        assert state["positions"][0]["qty"] == 1.5
        # Fehler kommen als Text zurück
        bad = svc.handle({"type": "paper", "op": "order", "symbol": "MULTI:BTCUSDT",
                          "side": "buy", "kind": "market", "qty": "1"})  # fmt: skip
        assert not bad["ok"]
        assert "kombinierten" in bad["message"]
        bad = svc.handle({"type": "paper", "op": "order", "symbol": "BITUNIX:BTCUSDT",
                          "side": "buy", "kind": "limit", "qty": "abc"})  # fmt: skip
        assert bad["message"] == "Menge: keine Zahl"
        svc._write(svc._serialize())
    async with EventBus() as bus:
        again = PaperService(PaperConfig(), {}, bus, bridge, tmp_path)
        assert again.engine.acc == svc.engine.acc
