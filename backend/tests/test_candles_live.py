"""Kerzen: Abschluss ohne Folge-Trade, späte Trades, Abgleich-Umfang, kein verlorener Abschluss."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import msgpack

from cockbook.adapters.bitunix import MinuteCandleBuilder
from cockbook.adapters.common import MINUTE_NS, bars_to_fetch
from cockbook.config import AggregationConfig, ServerConfig, UiConfig
from cockbook.core.eventbus import EventBus
from cockbook.core.events import Candle, CandleBar, Side, Trade
from cockbook.core.instrument import Instrument
from cockbook.server.bridge import Bridge
from cockbook.server.feed import MarketFeed

INST = Instrument("bitunix", "BTCUSDT", Decimal("0.1"), Decimal("0.001"))


def trade(ts_ns: int, ticks: int, qty: str = "1") -> Trade:
    return Trade(exchange="bitunix", symbol="BTCUSDT", ts_recv_ns=ts_ns, ts_exchange_ns=ts_ns,
                 price_ticks=ticks, qty=Decimal(qty), aggressor=Side.BID)  # fmt: skip


def test_roll_closes_minute_without_next_trade() -> None:
    b = MinuteCandleBuilder(INST)
    t0 = 100 * MINUTE_NS
    b.on_trade(trade(t0 + 5, 1000))
    b.on_trade(trade(t0 + 10, 1010))
    assert b.roll(t0 + MINUTE_NS - 1) == []  # Minute läuft noch
    [ev] = b.roll(t0 + MINUTE_NS + 2_000_000_000)
    assert isinstance(ev, Candle)
    assert ev.closed
    assert (ev.bar.open, ev.bar.high, ev.bar.close) == (Decimal(100), Decimal(101), Decimal(101))
    # verspäteter Trade der abgeschlossenen Minute ändert nichts mehr
    assert b.on_trade(trade(t0 + 50, 990)) == []
    # nächster Trade öffnet die neue Minute, ohne die alte erneut abzuschließen
    out = b.on_trade(trade(t0 + MINUTE_NS + 3, 1005))
    assert [e.closed for e in out if isinstance(e, Candle)] == [False]
    assert out[0].bar.open_time_ns == t0 + MINUTE_NS  # type: ignore[attr-defined]


def test_bars_to_fetch_covers_gaps() -> None:
    now = 1000 * MINUTE_NS
    assert bars_to_fetch(None, now, 3, 1500) == 3
    assert bars_to_fetch(now - MINUTE_NS, now, 3, 1500) == 4
    # 2 h ohne Abgleich (Verbindung weg, Ruhezustand): alles nachholen
    assert bars_to_fetch(now - 120 * MINUTE_NS, now, 3, 1500) == 123
    assert bars_to_fetch(now - 10_000 * MINUTE_NS, now, 3, 1500) == 1500


async def test_feed_keeps_close_of_previous_minute() -> None:
    """Abschluss von Minute M und Beginn von M+1 im selben Sendeintervall: beide gehen raus."""
    bridge = Bridge(ServerConfig())
    sent: list[dict[str, Any]] = []
    bridge.send_live = lambda sym, frame: sent.append(msgpack.unpackb(frame))  # type: ignore[method-assign,assignment]
    async with EventBus() as bus:
        feed = MarketFeed({"BTCUSDT": None}, AggregationConfig(), UiConfig(), bus, bridge)
        m = 10 * MINUTE_NS
        base: dict[str, Any] = {
            "exchange": "binance_usdm",
            "symbol": "BTCUSDT",
            "ts_recv_ns": 1,
            "interval": "1m",
        }
        closed = CandleBar(m, Decimal(1), Decimal(3), Decimal(1), Decimal(2), Decimal(5))
        forming = CandleBar(
            m + MINUTE_NS, Decimal(2), Decimal(2), Decimal(2), Decimal(2), Decimal(1)
        )
        await feed._on_chart_event(Candle(**base, bar=closed, closed=True))
        await feed._on_chart_event(Candle(**base, bar=forming, closed=False))
        feed._flush_candles()
    xs = [f["x"] for f in sent if f["t"] == "k"]
    assert [x[0] for x in xs] == [m // 1_000_000, (m + MINUTE_NS) // 1_000_000]
    assert xs[0][2] == 3.0  # Hoch der abgeschlossenen Kerze
    assert xs[0][-1] is True
