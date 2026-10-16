"""Mehrere Coins: automatische Bucket-Größe, Kennzahlen, Symbolwechsel in der Bridge."""

import asyncio
import json
import socket
from decimal import Decimal
from typing import Any

import aiohttp
import msgpack
import pytest

from cockbook.adapters.binance import (
    parse_long_short,
    parse_mark_price,
    parse_mini_ticker,
    parse_open_interest,
    parse_taker_volume,
)
from cockbook.config import ServerConfig
from cockbook.core.aggregation import HeatmapAggregator, auto_bucket_ticks, nice_ticks
from cockbook.core.events import (
    BookSnapshot,
    BookSyncStatus,
    InstrumentInfo,
    PriceLevel,
)
from cockbook.server.bridge import Bridge
from cockbook.server.protocol import encode_stat


@pytest.mark.parametrize(
    ("raw", "nice"),
    [(0.3, 1), (1.0, 1), (1.4, 1), (1.6, 2), (3.1, 2), (3.3, 5), (7.5, 10), (48, 50)],
)
def test_nice_ticks(raw: float, nice: int) -> None:
    assert nice_ticks(raw) == nice


@pytest.mark.parametrize(
    ("mid_price", "tick", "expected"),
    [
        ("80000", "0.10", 5),  # 80000 * 0.06bp = 0.48 USDT -> 4.8 Ticks -> 5
        ("3000", "0.01", 2),  # 0.018 USDT -> 1.8 Ticks -> 2
        ("150", "0.01", 1),  # unter 1 Tick -> 1
        ("2.5", "0.0001", 1),
    ],
)
def test_auto_bucket_per_coin(mid_price: str, tick: str, expected: int) -> None:
    t = Decimal(tick)
    mid_ticks = int(Decimal(mid_price) / t)
    assert auto_bucket_ticks(mid_ticks, t, 0.06) == expected


def test_aggregator_fixes_bucket_on_first_snapshot() -> None:
    c: dict[str, Any] = {"exchange": "x", "symbol": "ETHUSDT", "ts_recv_ns": 0}
    agg = HeatmapAggregator(bucket_ticks=None, half_range_buckets=10, bucket_bps=0.06)
    assert not agg.ready
    agg.on_event(InstrumentInfo(**c, tick_size=Decimal("0.01"), step_size=Decimal("0.001")))
    agg.on_event(
        BookSnapshot(
            **c,
            bids=(PriceLevel(299_999, Decimal(1)),),
            asks=(PriceLevel(300_001, Decimal(1)),),
            last_seq=1,
        )
    )
    agg.on_event(BookSyncStatus(**c, synced=True, reason=""))
    assert agg.ready
    assert agg.bucket_ticks == 2
    col = agg.make_column(0)
    assert col.synced
    assert len(col.values) == 20


def test_parse_market_stats() -> None:
    mp = parse_mark_price(
        {"e": "markPriceUpdate", "E": 1, "s": "BTCUSDT", "p": "80000.1", "i": "79990.5",
         "r": "0.00010000", "T": 1800000000000},
        "BTCUSDT", 5,
    )  # fmt: skip
    assert mp.funding_rate == Decimal("0.0001")
    assert mp.next_funding_ns == 1_800_000_000_000_000_000
    tk = parse_mini_ticker(
        {"e": "24hrMiniTicker", "E": 1, "s": "BTCUSDT", "c": "2", "o": "1", "h": "3",
         "l": "0.5", "v": "10", "q": "20"},
        "BTCUSDT", 5,
    )  # fmt: skip
    assert (tk.last, tk.open, tk.quote_volume) == (Decimal(2), Decimal(1), Decimal(20))
    oi = parse_open_interest({"openInterest": "10659.509", "symbol": "BTCUSDT", "time": 1}, 5)
    assert oi.open_interest == Decimal("10659.509")
    ls = parse_long_short(
        [{"symbol": "BTCUSDT", "longShortRatio": "0.1960", "longAccount": "0.6622",
          "shortAccount": "0.3378", "timestamp": 1}],
        "BTCUSDT", "5m", 5,
    )  # fmt: skip
    assert ls is not None
    assert ls.long_share == Decimal("0.6622")
    tv = parse_taker_volume(
        [{"buySellRatio": "1.5586", "buyVol": "387.33", "sellVol": "248.503", "timestamp": 1}],
        "BTCUSDT", "5m", 5,
    )  # fmt: skip
    assert tv is not None
    assert tv.ratio == Decimal("1.5586")
    assert parse_long_short([], "BTCUSDT", "5m", 5) is None


def test_encode_stat_roundtrip() -> None:
    oi = parse_open_interest({"openInterest": "12.5", "symbol": "SOLUSDT", "time": 1_000}, 5)
    enc = encode_stat(oi)
    assert enc is not None
    kind, frame = enc
    assert kind == "oi"
    assert msgpack.unpackb(frame) == {"t": "oi", "sym": "SOLUSDT", "ts": 1_000, "oi": 12.5}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_bridge_symbol_switch_and_stats_for_all() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port, history_columns=5))
    for sym in ("BTCUSDT", "ETHUSDT"):
        bridge.register_symbol(sym)
        bridge.set_hello(sym, json.dumps({"type": "hello", "symbol": sym}))
        bridge.publish(sym, [msgpack.packb({"t": "col", "sym": sym, "ts": 1})])
    bridge.publish_stat("ETHUSDT", "oi", msgpack.packb({"t": "oi", "sym": "ETHUSDT", "oi": 1.0}))
    await bridge.start()
    try:
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:

            async def recv() -> Any:
                msg = await ws.receive()
                if msg.type is aiohttp.WSMsgType.TEXT:
                    return json.loads(msg.data)
                return msgpack.unpackb(msg.data)

            async with asyncio.timeout(5):
                got = [await recv() for _ in range(4)]
                # Symbolliste, Kennzahl (auch für nicht angezeigtes ETH), Default = erstes Symbol
                assert got[0]["type"] == "symbols"
                assert [x["symbol"] for x in got[0]["symbols"]] == ["BTCUSDT", "ETHUSDT"]
                assert got[1]["sym"] == "ETHUSDT"
                assert got[2] == {"type": "hello", "symbol": "BTCUSDT"}
                assert got[3]["sym"] == "BTCUSDT"

                await ws.send_str(json.dumps({"type": "subscribe", "symbol": "ETHUSDT"}))
                got = [await recv() for _ in range(4)]
                assert got[2] == {"type": "hello", "symbol": "ETHUSDT"}
                assert got[3] == {"t": "col", "sym": "ETHUSDT", "ts": 1}

                # Live: nur das abonnierte Symbol kommt an
                bridge.publish("BTCUSDT", [msgpack.packb({"t": "col", "sym": "BTCUSDT", "ts": 2})])
                bridge.publish("ETHUSDT", [msgpack.packb({"t": "col", "sym": "ETHUSDT", "ts": 2})])
                assert await recv() == {"t": "col", "sym": "ETHUSDT", "ts": 2}
    finally:
        await bridge.stop()
