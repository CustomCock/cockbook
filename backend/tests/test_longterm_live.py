"""Laufende (vorläufige) Langzeit-Spalte."""

from __future__ import annotations

import msgpack

from cockbook.config import AggregationConfig, ServerConfig, UiConfig
from cockbook.core.aggregation import LongTermAccumulator
from cockbook.core.eventbus import EventBus
from cockbook.core.events import Side
from cockbook.server.bridge import Bridge
from cockbook.server.feed import MarketFeed


def test_peek_does_not_reset() -> None:
    acc = LongTermAccumulator(factor=2)
    acc.add_sample({10: 4.0}, 100, 101)
    acc.add_liquidation(Side.ASK, 50.0)
    first = acc.peek(1)
    assert dict(first.items()) == {10: 2.0}  # 4 / 1 Abtastung / Faktor 2
    acc.add_sample({10: 8.0}, 100, 101)
    assert dict(acc.peek(2).items()) == {10: 3.0}
    assert acc.liquidations == (50.0, 0.0)
    assert dict(acc.take(3).items()) == {10: 3.0}
    assert not acc.has_samples


async def test_feed_provisional_frame() -> None:
    async with EventBus() as bus:
        feed = MarketFeed(
            {"BTCUSDT": 5}, AggregationConfig(), UiConfig(), bus, Bridge(ServerConfig())
        )
        tier = feed.tiers["1M"]
        assert feed._provisional_frame("BTCUSDT", tier, 0) is None  # noch keine Abtastung
        feed._long["BTCUSDT"]["1M"].add_sample({7: 100.0}, 1, 2)
        now = 1_800_000_000_000 + 5
        frame = feed._provisional_frame("BTCUSDT", tier, now)
        assert frame is not None
        f = msgpack.unpackb(frame)
        assert f["p"] is True
        assert f["ts"] == 3_600_000  # Ende des laufenden 30-min-Intervalls


async def test_forming_bar_from_1m_candles() -> None:
    from decimal import Decimal

    from cockbook.core.events import CandleBar

    def bar(minute: int, o: int, h: int, low: int, c: int) -> CandleBar:
        d = Decimal
        return CandleBar(minute * 60_000_000_000, d(o), d(h), d(low), d(c), d(1))

    async with EventBus() as bus:
        feed = MarketFeed(
            {"BITUNIX:X": 5}, AggregationConfig(), UiConfig(), bus, Bridge(ServerConfig())
        )
        closed = [bar(0, 10, 12, 9, 11)]  # 5m-Kerze 0..5
        feed._candles["BITUNIX:X"] = {
            b.open_time_ns: b for b in (bar(5, 11, 13, 11, 12), bar(6, 12, 15, 10, 14))
        }
        out = feed._with_forming_bar("BITUNIX:X", closed, 5)
        assert len(out) == 2
        f = out[-1]
        assert f.open_time_ns == 300_000_000_000
        expected = [Decimal(v) for v in (11, 15, 10, 14, 2)]
        assert [f.open, f.high, f.low, f.close, f.volume] == expected
        # Lücke (1m-Kerzen beginnen erst später) -> nichts anhängen
        feed._candles["BITUNIX:X"] = {b.open_time_ns: b for b in (bar(11, 1, 1, 1, 1),)}
        assert feed._with_forming_bar("BITUNIX:X", closed, 5) == closed
