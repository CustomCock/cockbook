"""Kombiniertes Buch (Multibook) und Bitunix-Kerzen."""

from __future__ import annotations

from array import array
from decimal import Decimal
from typing import Any

import pytest

from cockbook.adapters.bitunix import MinuteCandleBuilder, parse_klines
from cockbook.core.aggregation import HeatmapColumn, TradeBin
from cockbook.core.combined import CombinedBook, decimal_gcd
from cockbook.core.events import Candle, Side, Trade
from cockbook.core.instrument import Instrument


def sparse(base: int, vals: dict[int, float], bb: int, ba: int, **kw: object) -> HeatmapColumn:
    keys = sorted(vals)
    return HeatmapColumn(
        1, True, base, array("f", (vals[k] for k in keys)), bb, ba,
        idx=array("i", (k - base for k in keys)), center=base, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def test_decimal_gcd() -> None:
    assert decimal_gcd(Decimal("0.1"), Decimal("0.01")) == Decimal("0.01")
    assert decimal_gcd(Decimal("0.5"), Decimal("0.2")) == Decimal("0.1")
    assert decimal_gcd(Decimal(1), Decimal("0.25")) == Decimal("0.25")
    with pytest.raises(ValueError, match="> 0"):
        decimal_gcd(Decimal(0), Decimal(1))


def test_same_ticks_sums_buckets_and_merges_bbo() -> None:
    book = CombinedBook([(Decimal("0.1"), 5), (Decimal("0.1"), 5)], half_range_buckets=100)
    assert book.tick_size == Decimal("0.1")
    assert book.bucket_ticks == 5
    a = sparse(100, {100: 1.0, 101: 2.0}, bb=504, ba=506)
    b = sparse(100, {101: 3.0, 103: 4.0}, bb=505, ba=508)
    c = book.combine(1, [a, b])
    assert c.synced
    assert dict(c.items()) == {100: 1.0, 101: 5.0, 103: 4.0}
    assert (c.best_bid, c.best_ask) == (505, 506)  # bester Bid/Ask über beide Börsen


def test_different_ticks_map_into_common_grid() -> None:
    # Börse A: Tick 0.1, Bucket 5 Ticks (=0.5); Börse B: Tick 0.01, Bucket 10 Ticks (=0.1)
    book = CombinedBook([(Decimal("0.1"), 5), (Decimal("0.01"), 10)], half_range_buckets=1000)
    assert book.tick_size == Decimal("0.01")
    assert book.bucket_ticks == 50  # gröberer Bucket: 0.5 = 50 gemeinsame Ticks
    # A: Bucket 200 = Preis 100.0–100.5; B: Buckets 1000..1004 = 100.0–100.5, 1005 = 100.5
    a = sparse(200, {200: 1.0}, bb=2000, ba=2001)
    b = sparse(1000, {1000: 1.0, 1004: 2.0, 1005: 4.0}, bb=20000, ba=20010)
    c = book.combine(1, [a, b])
    assert dict(c.items()) == {200: 4.0, 201: 4.0}
    assert (c.best_bid, c.best_ask) == (20000, 20010)


def test_combined_unsynced_if_any_venue_unsynced() -> None:
    book = CombinedBook([(Decimal("0.1"), 5), (Decimal("0.1"), 5)], half_range_buckets=100)
    a = sparse(100, {100: 1.0}, bb=504, ba=506)
    off = HeatmapColumn(1, False, 0, array("f"), None, None)
    assert not book.combine(1, [a, off]).synced
    with pytest.raises(ValueError, match="mindestens zwei"):
        CombinedBook([(Decimal(1), 1)], 10)


def test_trades_merged_with_converted_vwap() -> None:
    book = CombinedBook([(Decimal("0.1"), 5), (Decimal("0.01"), 50)], half_range_buckets=100)
    ta = TradeBin(Side.BID, qty=1.0, notional_ticks=1000.0, count=1)  # VWAP 1000 * 0.1 = 100.0
    tb = TradeBin(Side.BID, qty=1.0, notional_ticks=10010.0, count=2)  # 10010 * 0.01 = 100.1
    a = sparse(200, {200: 1.0}, bb=1000, ba=1001, trades=[ta])
    b = sparse(200, {200: 1.0}, bb=10000, ba=10010, trades=[tb])
    c = book.combine(1, [a, b])
    assert len(c.trades) == 1
    t = c.trades[0]
    assert (t.qty, t.count) == (2.0, 3)
    assert t.vwap_ticks == pytest.approx(10005.0)


def test_parse_klines_sorted_ascending() -> None:
    data = [
        {"open": "2", "high": "3", "low": "1", "close": "2.5", "baseVol": "7", "time": "120000"},
        {"open": "1", "high": "2", "low": "1", "close": "2", "baseVol": "5", "time": "60000"},
    ]
    bars = parse_klines(data)
    assert [b.open_time_ns for b in bars] == [60_000_000_000, 120_000_000_000]
    assert bars[1].close == Decimal("2.5")


def test_minute_candles_from_trades() -> None:
    inst = Instrument("bitunix", "BTCUSDT", Decimal("0.1"), Decimal("0.001"))
    cb = MinuteCandleBuilder(inst)

    def tr(sec: float, ticks: int, qty: str) -> list[Candle]:
        t = Trade(exchange="bitunix", symbol="BTCUSDT", ts_recv_ns=0,
                  ts_exchange_ns=int(sec * 1e9), price_ticks=ticks, qty=Decimal(qty),
                  aggressor=Side.BID)  # fmt: skip
        out = cb.on_trade(t)
        assert all(isinstance(e, Candle) for e in out)
        return out  # type: ignore[return-value]

    tr(60.5, 1000, "1")
    tr(70, 1010, "2")
    ev = tr(80, 990, "1")
    b = ev[-1].bar
    assert (b.open, b.high, b.low, b.close, b.volume) == (
        Decimal(100),
        Decimal(101),
        Decimal(99),
        Decimal(99),
        Decimal(4),
    )
    ev = tr(121, 1000, "1")  # neue Minute -> alte abgeschlossen
    assert [e.closed for e in ev] == [True, False]
    assert ev[1].bar.open_time_ns == 120_000_000_000
    assert tr(100, 1, "1") == []  # verspäteter Trade der alten Minute


async def test_feed_offers_and_builds_combined_book() -> None:
    import json

    from cockbook.config import AggregationConfig, ServerConfig, UiConfig
    from cockbook.core.eventbus import EventBus
    from cockbook.core.events import (
        BookSnapshot,
        BookSyncStatus,
        Event,
        InstrumentInfo,
        PriceLevel,
    )
    from cockbook.server.bridge import Bridge
    from cockbook.server.feed import MarketFeed

    bridge = Bridge(ServerConfig())
    async with EventBus() as bus:
        feed = MarketFeed({}, AggregationConfig(), UiConfig(), bus, bridge)
        venues = (("binance_usdm", "0.1", 1000, 1001), ("bitunix", "0.01", 10002, 10012))
        for ex, tick, bid, ask in venues:
            base: dict[str, Any] = {"exchange": ex, "symbol": "BTCUSDT", "ts_recv_ns": 1}
            events: list[Event] = [
                InstrumentInfo(**base, tick_size=Decimal(tick), step_size=Decimal("0.001")),
                BookSnapshot(
                    **base,
                    bids=(PriceLevel(bid, Decimal(2)),),
                    asks=(PriceLevel(ask, Decimal(3)),),
                    last_seq=1,
                ),
                BookSyncStatus(**base, synced=True, reason=""),
            ]
            for e in events:
                await feed._on_chart_event(e)
        syms = {s["symbol"]: s for s in json.loads(bridge._symbols_msg())["symbols"]}
        assert syms["MULTI:BTCUSDT"]["exchange"] == "MULTI"
        assert "BITUNIX:BTCUSDT" in syms
        cols = {k: a.make_column(10) for k, a in feed.aggs.items()}
        [(key, col)] = feed._combined_columns(10, cols)
        assert key == "MULTI:BTCUSDT"
        assert col.synced
        # gemeinsamer Tick 0.01: Binance-Bid 100.0 -> 10000, Bitunix-Bid 100.02 -> 10002
        assert (col.best_bid, col.best_ask) == (10002, 10010)
        assert sum(v for _, v in col.items()) == 10.0
