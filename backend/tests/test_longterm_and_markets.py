"""Langzeit-Heatmap, Gesamtmarkt-Kennzahlen, Orderbücher bei Bedarf, Symbol-Reihenfolge."""

import asyncio
import json
from array import array
from decimal import Decimal
from typing import Any, ClassVar

import aiohttp
import msgpack
import pytest

from cockbook import app as app_mod
from cockbook.adapters.binance import BinanceMarketStats, is_tradfi
from cockbook.config import AppConfig, BinanceUsdmConfig, HistoryConfig, ServerConfig, SymbolConfig
from cockbook.core.aggregation import HeatmapColumn, LongTermAccumulator
from cockbook.core.eventbus import EventBus
from cockbook.core.events import Event, MarketSession, MarkPrice, Ticker24h
from cockbook.core.instrument import Instrument
from cockbook.server.bridge import Bridge
from cockbook.server.protocol import encode_control, encode_long_column, encode_session
from tests.test_server import free_port

# --- LongTermAccumulator ---------------------------------------------------------------


def test_tier_accumulator_mean_density_and_liquidations() -> None:
    from cockbook.core.events import Side

    acc = LongTermAccumulator(4)
    acc.add_sample({10: 8.0, 12: 4.0}, 1, 2)
    acc.add_sample({10: 0.0, 11: 8.0}, 3, 4)
    acc.add_sample(None, 5, 6)  # nicht synchron -> zählt nicht
    acc.add_liquidation(Side.ASK, 1000.0)  # Long liquidiert
    acc.add_liquidation(Side.BID, 250.0)
    out = acc.take(7)
    assert out.synced
    # Mittel über 2 Abtastungen, als Dichte je feinem Bucket (/4)
    assert dict(out.items()) == {10: 1.0, 11: 1.0, 12: 0.5}
    assert (out.best_bid, out.best_ask) == (3, 4)
    assert acc.take_liquidations() == (1000.0, 250.0)
    assert acc.take_liquidations() == (0.0, 0.0)
    assert not acc.take(8).synced  # zurückgesetzt
    with pytest.raises(ValueError, match="factor"):
        LongTermAccumulator(0)


def test_sparse_column_and_long_frames() -> None:
    import struct

    from cockbook.core.events import CandleBar
    from cockbook.server.protocol import encode_column, encode_klines

    c = HeatmapColumn(5_000_000, True, 100, array("f", [1.5, 2.0]), 3, 4, idx=array("i", [0, 7]))
    f = msgpack.unpackb(encode_column("BTCUSDT", c)[0])
    assert struct.unpack("<2i", f["i"]) == (0, 7)
    assert struct.unpack("<2f", f["q"]) == (1.5, 2.0)
    lc = msgpack.unpackb(encode_long_column("BTCUSDT", "1D", c, 5, 16, 1234.567, 8.0))
    assert (lc["t"], lc["tier"], lc["bt"], lc["f"], lc["ts"], lc["b"]) == (
        "lc",
        "1D",
        5,
        16,
        5,
        100,
    )
    assert (lc["ll"], lc["lsh"]) == (1234.57, 8.0)
    bar = CandleBar(open_time_ns=60_000_000_000, open=Decimal(1), high=Decimal(2),
                    low=Decimal("0.5"), close=Decimal("1.5"), volume=Decimal(10))  # fmt: skip
    lk = msgpack.unpackb(encode_klines("BTCUSDT", "1D", "15m", [bar]))
    assert lk["x"] == [[60_000, 1.0, 2.0, 0.5, 1.5, 10.0]]
    s = msgpack.unpackb(
        encode_session(
            MarketSession(exchange="x", symbol="COMMODITY", ts_recv_ns=2_000_000,
                          session="NO_TRADING", start_ns=1_000_000, end_ns=None)
        )
    )  # fmt: skip
    assert s == {
        "t": "sess",
        "sym": "COMMODITY",
        "ts": 2,
        "session": "NO_TRADING",
        "start": 1,
        "end": None,
    }


# --- BinanceMarketStats -----------------------------------------------------------------


async def test_market_stats_parses_arrays_filters_cm_and_sessions() -> None:
    got: list[Event] = []
    async with EventBus() as bus:

        async def on(e: Event) -> None:
            got.append(e)

        for t in (MarkPrice, Ticker24h, MarketSession):
            bus.subscribe(t, on, name=t.__name__)
        stats = BinanceMarketStats(
            BinanceUsdmConfig(name="binance_usdm", symbols=[SymbolConfig(symbol="BTCUSDT")]),
            ["BTCUSDT"],
            bus,
        )
        stats.available["BTCUSDT"] = Instrument(
            exchange="binance_usdm",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
        )
        mark = {
            "e": "markPriceUpdate",
            "E": 1,
            "s": "BTCUSDT",
            "p": "1",
            "i": "1",
            "P": "1",
            "r": "0.0001",
            "T": 2,
        }
        await stats._on_message(
            json.dumps({"stream": "!markPrice@arr@1s", "data": [
                mark,
                {**mark, "s": "BTCUSD_PERP", "st": 2},  # coin-margined -> raus
                {**mark, "s": "ETHUSDT"},  # nicht konfiguriert -> raus
            ]}),
            10,
        )  # fmt: skip
        await stats._on_message(
            json.dumps({"stream": "!miniTicker@arr", "data": [
                {"e": "24hrMiniTicker", "E": 1, "s": "BTCUSDT", "c": "2", "o": "1",
                 "h": "2", "l": "1", "v": "1", "q": "1"}
            ]}),
            11,
        )  # fmt: skip
        await stats._on_message(
            json.dumps({"stream": "tradingSession", "data": {
                "e": "CommodityUpdate", "E": 5, "t": 1000, "T": 2000, "S": "REGULAR"
            }}),
            12,
        )  # fmt: skip
        await asyncio.sleep(0.05)
    kinds = sorted(type(e).__name__ for e in got)
    assert kinds == ["MarkPrice", "MarketSession", "Ticker24h"]
    sess = next(e for e in got if isinstance(e, MarketSession))
    assert (sess.symbol, sess.session, sess.start_ns, sess.end_ns) == (
        "COMMODITY", "REGULAR", 1_000_000_000, 2_000_000_000
    )  # fmt: skip


def test_is_tradfi() -> None:
    assert is_tradfi({"contractType": "TRADIFI_PERPETUAL"})
    assert not is_tradfi({"contractType": "PERPETUAL"})
    assert not is_tradfi({})
    assert not is_tradfi({"contractType": "PERPETUAL", "underlyingType": "COIN"})
    assert not is_tradfi({"contractType": "PERPETUAL", "underlyingType": "INDEX"})
    # QQQ & Co.: normales PERPETUAL, aber Aktien-Underlying bzw. in der Config
    assert is_tradfi({"contractType": "PERPETUAL", "underlyingType": "EQUITY"})
    assert is_tradfi({"symbol": "QQQUSDT", "contractType": "PERPETUAL"}, {"QQQUSDT": "etf"})


# --- SourceManager -------------------------------------------------------------------------


class FakeAdapter:
    started: ClassVar[list[str]] = []

    def __init__(self, cfg: BinanceUsdmConfig, symbol: str, bus: EventBus) -> None:
        self.symbol = symbol.upper()
        self.name = f"binance_usdm:{self.symbol}"
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        FakeAdapter.started.append(self.symbol)
        if self.symbol == "BADUSDT":
            raise RuntimeError("delisted")
        await self._stop.wait()


async def test_source_manager_on_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAdapter.started = []
    monkeypatch.setattr(app_mod, "BinanceUsdmAdapter", FakeAdapter)
    cfg = AppConfig(
        exchanges=[
            BinanceUsdmConfig(
                name="binance_usdm",
                symbols=[
                    SymbolConfig(symbol="BTCUSDT", always_on=True),
                    SymbolConfig(symbol="DOGEUSDT"),
                    SymbolConfig(symbol="BADUSDT"),
                ],
                liquidations_all_market=False,
            )
        ],
        history=HistoryConfig(enabled=False),
    )
    async with EventBus() as bus:
        mgr = app_mod.SourceManager(cfg, bus)
        # Gesamtmarkt-Quellen ohne Netz: durch Dummy ersetzen
        monkeypatch.setattr(mgr, "_market_sources", lambda: [])
        stop = asyncio.Event()
        task = asyncio.create_task(mgr.run(stop))
        await asyncio.sleep(0.05)
        assert FakeAdapter.started == ["BTCUSDT"]  # nur always_on
        mgr.activate("dogeusdt")
        mgr.activate("DOGEUSDT")  # idempotent
        mgr.activate("ETHUSDT")  # nicht konfiguriert -> ignoriert
        mgr.activate("BADUSDT")  # scheitert, darf den Rest nicht mitreißen
        await asyncio.sleep(0.05)
        assert FakeAdapter.started == ["BTCUSDT", "DOGEUSDT", "BADUSDT"]
        assert mgr.active == {"BTCUSDT", "DOGEUSDT"}
        mgr.activate("BADUSDT")  # nach Fehler erneut startbar
        await asyncio.sleep(0.05)
        assert FakeAdapter.started.count("BADUSDT") == 2
        stop.set()
        async with asyncio.timeout(2):
            await task


# --- Bridge: Reihenfolge, Abo-Hook, Langzeit-Historie ---------------------------------------


async def test_bridge_order_hook_and_long_view() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port, history_columns=2))
    subscribed: list[str] = []
    bridge.on_subscribe = subscribed.append
    requests: list[tuple[str, str, str]] = []

    async def handler(sym: str, tier: str, what: str) -> list[str | bytes]:
        requests.append((sym, tier, what))
        return [encode_control("long_hello", symbol=sym, tier=tier), b"\x81\xa1t\xa2lc"]

    bridge.long_handler = handler
    bridge.set_symbol_order(["BTCUSDT", "XAUUSDT"])
    await bridge.start()
    try:
        bridge.register_symbol("XAUUSDT", kind="tradfi", label="Gold")
        bridge.register_symbol("BTCUSDT", kind="crypto", label="BTC")
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            first = json.loads((await ws.receive()).data)
            assert [x["symbol"] for x in first["symbols"]] == ["BTCUSDT", "XAUUSDT"]
            assert first["symbols"][1]["kind"] == "tradfi"
            await ws.send_str(json.dumps({"type": "long", "symbol": "XAUUSDT", "tier": "1D"}))
            got: list[Any] = []
            async with asyncio.timeout(5):
                while len(got) < 3:
                    msg = await ws.receive()
                    data = (
                        json.loads(msg.data)
                        if msg.type is aiohttp.WSMsgType.TEXT
                        else msgpack.unpackb(msg.data)
                    )
                    if data.get("type") == "long_hello" or data.get("t") == "lc":
                        got.append(data)
                    if len(got) == 2:
                        # neue Spalte der abonnierten Stufe kommt live; andere Stufe nicht
                        bridge.publish_tier("XAUUSDT", "1W", b"\x81\xa1t\xa2xx")
                        bridge.publish_tier("XAUUSDT", "1D", b"\x81\xa1t\xa2lc")
        assert got[0]["type"] == "long_hello"
        assert [g.get("t") for g in got[1:]] == ["lc", "lc"]
        assert requests == [("XAUUSDT", "1D", "all")]
        assert subscribed == ["BTCUSDT", "XAUUSDT"]  # Standard-Abo + Orderbuch für Langzeit
    finally:
        await bridge.stop()


# --- Langzeit: breite Abtastung, Archiv, Klassifizierung ------------------------------------


def test_coarse_sums_are_wider_than_fine_column() -> None:
    from cockbook.core.aggregation import HeatmapAggregator
    from cockbook.core.events import BookSnapshot, BookSyncStatus, InstrumentInfo, PriceLevel

    c: dict[str, Any] = {"exchange": "x", "symbol": "S", "ts_recv_ns": 0}
    agg = HeatmapAggregator(bucket_ticks=5, half_range_buckets=2)
    agg.on_event(InstrumentInfo(**c, tick_size=Decimal("0.1"), step_size=Decimal("1")))
    agg.on_event(
        BookSnapshot(
            **c,
            bids=(PriceLevel(99, Decimal(8)), PriceLevel(20, Decimal(40))),
            asks=(PriceLevel(100, Decimal(4)),),
            last_seq=1,
        )
    )
    agg.on_event(BookSyncStatus(**c, synced=True, reason=""))
    assert [b for b, _ in agg.make_column(0).items()] == [19, 20]  # fein: nur +-2 Buckets
    # grob: Bucket = 4 feine = 20 Ticks, weit -> auch die Wall bei Tick 20 (Summe, keine Dichte)
    assert agg.coarse_sums(4, 100) == {1: 40.0, 4: 8.0, 5: 4.0}


def test_archive_roundtrip_skips_other_scale_and_prunes(tmp_path: Any) -> None:
    from cockbook.server.longterm_archive import LongTermArchive

    day = 86_400_000
    now = 20_000 * day + 5_000
    a = LongTermArchive(tmp_path, "binance_usdm", "1D", retention_ms=2 * day)

    def mk(ts: int, bt: int) -> bytes:
        c = HeatmapColumn(ts * 1_000_000, True, 7, array("f", [1.0]), 1, 2)
        return encode_long_column("BTCUSDT", "1D", c, bt, 4)

    a.append("BTCUSDT", now - 3 * day, mk(now - 3 * day, 5))  # zu alt
    a.append("BTCUSDT", now - day, mk(now - day, 5))
    a.append("BTCUSDT", now - 1000, mk(now - 1000, 5))
    a.append("BTCUSDT", now - 500, mk(now - 500, 2))  # andere Bucket-Größe
    a.append("BTCUSDT", now - 1000, mk(now - 1000, 5))  # Duplikat
    # abgeschnittener Frame am Dateiende (Absturz beim Schreiben)
    path = tmp_path / "longterm" / "binance_usdm" / "BTCUSDT" / "1D"
    with sorted(path.glob("*.msgpack"))[-1].open("ab") as f:
        f.write(mk(now, 5)[:10])
    frames = a.load("BTCUSDT", now, 5, 4)
    assert [msgpack.unpackb(f)["ts"] for f in frames] == [now - day, now - 1000]
    # Orderbuch läuft nicht (Bucket-Größe unbekannt): die des neuesten Frames gilt
    assert [msgpack.unpackb(f)["ts"] for f in a.load("BTCUSDT", now, None, 4)] == [now - 500]
    a.prune("BTCUSDT", now)
    assert len(list(path.glob("*.msgpack"))) == 2


def test_classify_tradfi() -> None:
    from cockbook.adapters.binance import classify

    tf = {"contractType": "TRADIFI_PERPETUAL"}
    assert classify({"symbol": "BTCUSDT", "contractType": "PERPETUAL"}) == (
        "crypto",
        "crypto",
        None,
    )
    assert classify({**tf, "symbol": "TSLAUSDT", "underlyingType": "EQUITY"}) == (
        "tradfi",
        "stock",
        "EQUITY",
    )
    assert classify({**tf, "symbol": "X", "underlyingType": "KR_EQUITY"})[1:] == (
        "stock",
        "KR_EQUITY",
    )
    assert classify({**tf, "symbol": "XAUUSDT", "underlyingType": "COMMODITY"})[1:] == (
        "commodity",
        "COMMODITY",
    )
    assert classify({**tf, "symbol": "EURUSDT", "underlyingType": "FX"})[1] == "fx"
    assert (
        classify({**tf, "symbol": "Q", "underlyingType": "EQUITY", "underlyingSubType": ["ETF"]})[1]
        == "etf"
    )
    assert (
        classify({**tf, "symbol": "EWJUSDT", "underlyingType": "EQUITY"}, {"EWJUSDT": "etf"})[1]
        == "etf"
    )
    assert classify({**tf, "symbol": "Z", "underlyingType": "SOMETHING_NEW"})[1] == "tradfi"
    qqq = {"symbol": "QQQUSDT", "contractType": "PERPETUAL", "underlyingType": "COIN"}
    assert classify(qqq, {"QQQUSDT": "etf"}) == ("tradfi", "etf", "EQUITY")


async def test_bridge_since_sends_only_missing_history() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port, history_columns=10))
    await bridge.start()
    try:
        bridge.register_symbol("BTCUSDT")
        bridge.set_hello("BTCUSDT", encode_control("hello", symbol="BTCUSDT"))
        for i in range(1, 4):
            bridge.publish("BTCUSDT", [msgpack.packb({"t": "col", "ts": i})], i)
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            await ws.send_str(json.dumps({"type": "subscribe", "symbol": "BTCUSDT", "since": 2}))
            got: list[Any] = []
            async with asyncio.timeout(5):
                while True:
                    msg = await ws.receive()
                    if msg.type is aiohttp.WSMsgType.BINARY:
                        got.append(msgpack.unpackb(msg.data)["ts"])
                    if got[-1:] == [3] and got.count(3) == 2:
                        break
        assert got == [1, 2, 3, 3]  # erst Standard-Abo (alles), dann nur das Fehlende
    finally:
        await bridge.stop()


async def test_feed_long_request_serves_archive(tmp_path: Any) -> None:
    import time

    from cockbook.config import AggregationConfig, UiConfig
    from cockbook.server.feed import MarketFeed, _interval_minutes
    from cockbook.server.longterm_archive import LongTermArchive

    agg_cfg = AggregationConfig()
    tier = next(t for t in agg_cfg.long_tiers if t.name == "1D")
    now_ms = time.time_ns() // 1_000_000
    arch = LongTermArchive(tmp_path, "binance_usdm", "1D", 90_000_000)
    for k in (3, 2, 1):
        c = HeatmapColumn((now_ms - k * 60_000) * 1_000_000, True, 7, array("f", [1.0]), 1, 2,
                          idx=array("i", [0]))  # fmt: skip
        arch.append(
            "BTCUSDT", now_ms - k * 60_000, encode_long_column("BTCUSDT", "1D", c, 5, tier.factor)
        )
    async with EventBus() as bus:
        feed = MarketFeed(
            {}, agg_cfg, UiConfig(), bus, Bridge(ServerConfig()), archive_dir=tmp_path
        )
        out = await feed.long_request("BTCUSDT", "1D", "all")
        assert json.loads(out[0])["type"] == "long_hello"
        hello = json.loads(out[0])
        assert (hello["bucket_ticks"], hello["factor"], hello["columns"]) == (5, tier.factor, 3)
        assert [msgpack.unpackb(f)["t"] for f in out[1:4] if isinstance(f, bytes)] == ["lc"] * 3
        assert json.loads(out[-1])["type"] == "long_ready"
        bad = await feed.long_request("BTCUSDT", "2Y", "all")
        assert json.loads(bad[0])["type"] == "error"
    assert (_interval_minutes("15m"), _interval_minutes("4h"), _interval_minutes("x")) == (
        15,
        240,
        0,
    )
