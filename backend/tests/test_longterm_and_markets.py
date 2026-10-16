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


def col(base: int, values: list[float], bb: int = 1, ba: int = 2) -> HeatmapColumn:
    return HeatmapColumn(0, True, base, array("f", values), bb, ba)


# --- LongTermAccumulator ---------------------------------------------------------------


def test_long_term_mean_over_time_and_buckets() -> None:
    acc = LongTermAccumulator(2)
    acc.add(col(10, [4.0, 0.0, 2.0, 2.0]))  # Buckets 10..13 -> grob 5, 5, 6, 6
    acc.add(col(10, [0.0, 0.0, 6.0, 2.0], bb=7, ba=8))
    out = acc.take(123)
    assert out.synced
    assert out.ts_ns == 123
    assert out.base_bucket == 5
    # grob 5: (4 + 0 + 0 + 0) / (2 Spalten * 2 Buckets) = 1; grob 6: (2+2+6+2)/4 = 3
    assert list(out.values[:2]) == [1.0, 3.0]
    assert (out.best_bid, out.best_ask) == (7, 8)  # letzter BBO des Intervalls


def test_long_term_shifted_window_and_reset() -> None:
    acc = LongTermAccumulator(2)
    acc.add(col(10, [2.0, 2.0]))
    acc.add(col(12, [2.0, 2.0]))  # Preis gewandert: landet eine grobe Stufe höher
    out = acc.take(1)
    assert list(out.values[:2]) == [1.0, 1.0]
    empty = acc.take(2)  # nach take ist alles zurückgesetzt
    assert not empty.synced
    assert len(empty.values) == 0


def test_long_term_ignores_unsynced_columns() -> None:
    acc = LongTermAccumulator(1)
    acc.add(HeatmapColumn(0, False, 0, array("f"), None, None))
    assert not acc.take(1).synced
    with pytest.raises(ValueError, match="factor"):
        LongTermAccumulator(0)


def test_long_column_and_session_frames() -> None:
    c = msgpack.unpackb(
        encode_long_column("BTCUSDT", HeatmapColumn(5_000_000, True, 7, array("f", [1.5]), 3, 4))
    )
    assert (c["t"], c["sym"], c["ts"], c["b"], c["bb"], c["ba"]) == ("cc", "BTCUSDT", 5, 7, 3, 4)
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


async def test_bridge_order_hook_and_long_history() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port, history_columns=2, long_history_columns=2))
    subscribed: list[str] = []
    bridge.on_subscribe = subscribed.append
    bridge.set_symbol_order(["BTCUSDT", "XAUUSDT"])
    await bridge.start()
    try:
        bridge.register_symbol("XAUUSDT", kind="tradfi", label="Gold")
        bridge.register_symbol("BTCUSDT", kind="crypto", label="BTC")
        bridge.set_hello("BTCUSDT", encode_control("hello", symbol="BTCUSDT"))
        for i in range(3):
            bridge.publish_long(
                "BTCUSDT",
                encode_long_column(
                    "BTCUSDT", HeatmapColumn(i * 1_000_000, True, 0, array("f", [1.0]), 1, 2)
                ),
            )
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            got: list[Any] = []
            async with asyncio.timeout(5):
                while len(got) < 4:
                    msg = await ws.receive()
                    got.append(
                        json.loads(msg.data)
                        if msg.type is aiohttp.WSMsgType.TEXT
                        else msgpack.unpackb(msg.data)
                    )
        assert [x["symbol"] for x in got[0]["symbols"]] == [
            "BTCUSDT",
            "XAUUSDT",
        ]  # Config-Reihenfolge
        assert got[0]["symbols"][1]["kind"] == "tradfi"
        assert got[1]["type"] == "hello"
        assert [g["ts"] for g in got[2:]] == [1, 2]  # Langzeit-Historie fasst 2
        assert subscribed == ["BTCUSDT"]  # Standard-Abo = erstes Symbol, Hook gerufen
    finally:
        await bridge.stop()


# --- Langzeit: breite Abtastung, Archiv, Klassifizierung ------------------------------------


def test_coarse_column_samples_wide_book_as_density() -> None:
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
    fine = agg.make_column(0)
    assert fine.base_bucket == 17  # fein: nur +-2 Buckets um den Mid
    coarse = agg.make_coarse_column(0, 4, 100)  # grob: Bucket = 20 Ticks, weit
    vals = {coarse.base_bucket + i: v for i, v in enumerate(coarse.values) if v}
    # Tick 20 -> grober Bucket 1, Wert als Dichte je feinem Bucket: 40 / 4 = 10
    assert vals == {1: 10.0, 4: 2.0, 5: 1.0}


def test_long_term_pad_keeps_moving_columns() -> None:
    acc = LongTermAccumulator(1, pad=100)
    acc.add(col(1000, [4.0]))
    acc.add(col(1050, [4.0]))  # Preis 50 Buckets weiter: früher abgeschnitten
    out = acc.take(1)
    assert out.base_bucket == 1000
    assert out.values[0] == 2.0
    assert out.values[50] == 2.0
    assert len(out.values) == 51  # leere Ränder abgeschnitten


def test_archive_roundtrip_skips_other_scale_and_prunes(tmp_path: Any) -> None:
    from cockbook.server.longterm_archive import LongTermArchive

    day = 86_400_000
    now = 20_000 * day + 5_000
    a = LongTermArchive(tmp_path, "binance_usdm", retention_ms=2 * day)

    def mk(ts: int, bt: int) -> bytes:
        c = HeatmapColumn(ts * 1_000_000, True, 7, array("f", [1.0]), 1, 2)
        return encode_long_column("BTCUSDT", c, bt, 4)

    a.append("BTCUSDT", now - 3 * day, mk(now - 3 * day, 5))  # zu alt
    a.append("BTCUSDT", now - day, mk(now - day, 5))
    a.append("BTCUSDT", now - 1000, mk(now - 1000, 5))
    a.append("BTCUSDT", now - 500, mk(now - 500, 2))  # andere Bucket-Größe
    a.append("BTCUSDT", now - 1000, mk(now - 1000, 5))  # Duplikat
    # abgeschnittener Frame am Dateiende (Absturz beim Schreiben)
    path = tmp_path / "longterm" / "binance_usdm" / "BTCUSDT"
    with sorted(path.glob("*.msgpack"))[-1].open("ab") as f:
        f.write(mk(now, 5)[:10])
    frames = a.load("BTCUSDT", now, 5, 4)
    assert [msgpack.unpackb(f)["ts"] for f in frames] == [now - day, now - 1000]
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


async def test_bridge_since_sends_only_missing_history() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port, history_columns=10, long_history_columns=10))
    await bridge.start()
    try:
        bridge.register_symbol("BTCUSDT")
        bridge.set_hello("BTCUSDT", encode_control("hello", symbol="BTCUSDT"))
        for i in range(1, 4):
            c = HeatmapColumn(i * 1_000_000, True, 0, array("f", [1.0]), 1, 2)
            bridge.publish_long("BTCUSDT", encode_long_column("BTCUSDT", c), i)
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            await ws.send_str(
                json.dumps({"type": "subscribe", "symbol": "BTCUSDT", "since_long": 2})
            )
            got: list[Any] = []
            async with asyncio.timeout(5):
                while True:
                    msg = await ws.receive()
                    if msg.type is aiohttp.WSMsgType.BINARY:
                        got.append(msgpack.unpackb(msg.data)["ts"])
                    # nach dem Resubscribe: nur noch Spalte 3
                    if got[-1:] == [3] and got.count(3) == 2:
                        break
        assert got == [1, 2, 3, 3]  # erst Standard-Abo (alles), dann nur das Fehlende
    finally:
        await bridge.stop()
