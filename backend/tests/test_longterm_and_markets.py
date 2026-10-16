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
