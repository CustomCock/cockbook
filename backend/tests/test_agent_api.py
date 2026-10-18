"""Abfragen für Claude: Orderbuch-Zusammenfassung, Trade-Flow, Kerzen, Kennzahlen."""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from typing import Any

from cockbook.config import AgentConfig
from cockbook.core.aggregation import HeatmapAggregator
from cockbook.core.events import (
    BookSnapshot,
    BookSyncStatus,
    CandleBar,
    Event,
    InstrumentInfo,
    PriceLevel,
    Side,
    Ticker24h,
    Trade,
)
from cockbook.server.agent_api import AgentApi


def agg(
    exchange: str, tick: str, bids: list[tuple[int, int]], asks: list[tuple[int, int]]
) -> HeatmapAggregator:
    a = HeatmapAggregator(bucket_ticks=1, half_range_buckets=100)
    base: dict[str, Any] = {"exchange": exchange, "symbol": "BTCUSDT", "ts_recv_ns": 1}
    a.on_event(InstrumentInfo(**base, tick_size=Decimal(tick), step_size=Decimal("0.001")))
    a.on_event(
        BookSnapshot(
            **base,
            last_seq=1,
            bids=tuple(PriceLevel(p, Decimal(q)) for p, q in bids),
            asks=tuple(PriceLevel(p, Decimal(q)) for p, q in asks),
        )
    )
    a.on_event(BookSyncStatus(**base, synced=True, reason=""))
    return a


class FakeFeed:
    def __init__(self) -> None:
        # Binance Tick 0.1, Bitunix Tick 1: Preis 100.0 auf beiden Börsen = gleiches Level
        self.aggs = {
            "BTCUSDT": agg("binance_usdm", "0.1", [(999, 2), (990, 10)], [(1000, 1), (1010, 4)]),
            "BITUNIX:BTCUSDT": agg("bitunix", "1", [(99, 3)], [(101, 1)]),
        }
        now = time.time_ns()
        base: dict[str, Any] = {"exchange": "binance_usdm", "symbol": "BTCUSDT"}
        self.tape: dict[str, deque[Trade]] = {
            "BTCUSDT": deque(
                [
                    Trade(
                        **base,
                        ts_recv_ns=now - 600 * 10**9,
                        price_ticks=1000,
                        qty=Decimal(9),
                        aggressor=Side.BID,
                    ),
                    Trade(
                        **base,
                        ts_recv_ns=now - 10 * 10**9,
                        price_ticks=1000,
                        qty=Decimal(2),
                        aggressor=Side.BID,
                    ),
                    Trade(
                        **base,
                        ts_recv_ns=now - 5 * 10**9,
                        price_ticks=999,
                        qty=Decimal("0.5"),
                        aggressor=Side.ASK,
                    ),
                ]
            ),
            "BITUNIX:BTCUSDT": deque(),
        }
        self.stats: dict[str, dict[str, Event]] = {
            "BTCUSDT": {
                "Ticker24h": Ticker24h(
                    **base,
                    ts_recv_ns=now,
                    last=Decimal(100),
                    open=Decimal(95),
                    high=Decimal(101),
                    low=Decimal(94),
                    volume=Decimal(10),
                    quote_volume=Decimal(1000),
                )
            }
        }
        m = 60 * 10**9
        self.bars = [
            CandleBar(
                open_time_ns=i * m,
                open=Decimal(i),
                high=Decimal(i + 1),
                low=Decimal(i - 1),
                close=Decimal(i),
                volume=Decimal(1),
            )
            for i in range(10)
        ]

    def legs(self, key: str) -> list[str]:
        if key == "MULTI:BTCUSDT":
            return ["BTCUSDT", "BITUNIX:BTCUSDT"]
        return [key] if key in self.aggs else []

    def candles_1m(self, key: str) -> list[CandleBar]:
        return self.bars


def api() -> AgentApi:
    return AgentApi(FakeFeed(), AgentConfig())  # type: ignore[arg-type]


def q(what: str, **kw: Any) -> dict[str, Any]:
    r = api().handle({"type": "query", "what": what, "req": 1, **kw})
    assert r["type"] == "query_result"
    assert r["req"] == 1
    return r


def test_symbols() -> None:
    rows = q("symbols")["data"]
    assert {
        "symbol": "BTCUSDT",
        "synced": True,
        "tick_size": 0.1,
        "bid": 99.9,
        "ask": 100.0,
    } in rows


def test_book_summary_and_walls() -> None:
    d = q("book", symbol="BTCUSDT", range_pct=0.5, walls=1)["data"]
    assert d["mid"] == 99.95
    assert d["spread"] == 0.1
    # ±0.5 % um 99.95 = 99.45 … 100.45: 99.0 (Bid 10) und 101.0 (Ask 4) liegen außerhalb
    assert d["bids"]["qty"] == 2
    assert d["asks"]["qty"] == 1
    assert d["bids"]["walls"] == [{"price": 99.9, "qty": 2, "usd": 200, "dist_pct": -0.05}]
    wide = q("book", symbol="BTCUSDT", range_pct=2, walls=5)["data"]
    assert wide["bids"]["walls"][0]["price"] == 99.0  # größte Wand zuerst


def test_book_multi_merges_venues_by_price() -> None:
    d = q("book", symbol="MULTI:BTCUSDT", range_pct=5, walls=10, group=1)["data"]
    assert d["venues"] == ["BTCUSDT", "BITUNIX:BTCUSDT"]
    bids = {w["price"]: w["qty"] for w in d["bids"]["walls"]}
    # Gruppe 99: Binance 99.9 (2) + 99.0 (10) + Bitunix 99 (3)
    assert bids == {99.0: 15}
    asks = {w["price"]: w["qty"] for w in d["asks"]["walls"]}
    assert asks == {100.0: 1, 101.0: 5}
    assert -1 <= d["imbalance"] <= 1


def test_trade_flow_window() -> None:
    d = q("trades", symbol="BTCUSDT", seconds=60)["data"]
    assert d["count"] == 2
    assert d["buy_qty"] == 2
    assert d["sell_qty"] == 0.5
    assert d["delta_qty"] == 1.5
    assert d["largest"][0]["side"] == "buy"
    assert d["largest"][0]["qty"] == 2
    assert d["covered_seconds"] == 60
    assert q("trades", symbol="BTCUSDT", seconds=3600)["data"]["count"] == 3


def test_candles_aggregate() -> None:
    d = q("candles", symbol="BTCUSDT", minutes=5, limit=10)["data"]
    assert d["candles"][0] == [0, 0.0, 5.0, -1.0, 4.0, 5.0]
    assert len(d["candles"]) == 2
    assert not q("candles", symbol="BTCUSDT", minutes=7)["ok"]


def test_stats_and_errors() -> None:
    d = q("stats", symbol="BTCUSDT")["data"]
    assert d["venues"]["BTCUSDT"]["Ticker24h"]["last"] == 100.0
    assert not q("book", symbol="DOGEUSDT")["ok"]
    assert not q("book")["ok"]
    assert not q("nonsense")["ok"]
    assert not q("book", symbol="BTCUSDT", range_pct=-1)["ok"]
