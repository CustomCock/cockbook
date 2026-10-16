"""Tageshistorie (Paginierung, Cache) und Kerzen-Parser."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from cockbook.adapters.binance import parse_kline_row, parse_kline_ws
from cockbook.adapters.binance_history import BinanceDailyHistory, merge_rows
from cockbook.config import HistoryConfig
from cockbook.core.eventbus import EventBus
from cockbook.core.events import CandleHistory

DAY = 86_400_000
T0 = 1_502_928_000_000  # 2017-08-17


def row(i: int, close: str = "100") -> list[Any]:
    t = T0 + i * DAY
    return [t, "1", "2", "0.5", close, "10", t + DAY - 1, "0", 0, "0", "0", "0"]


def test_merge_rows_newer_wins_and_sorted() -> None:
    merged = merge_rows([row(0), row(1, "5")], [row(1, "7"), row(2)])
    assert [r[0] for r in merged] == [T0, T0 + DAY, T0 + 2 * DAY]
    assert merged[1][4] == "7"  # laufende Kerze aktualisiert


def test_parse_kline_rest_and_ws() -> None:
    b = parse_kline_row(row(0, "123.45"))
    assert b.open_time_ns == T0 * 1_000_000
    assert b.close == Decimal("123.45")
    c = parse_kline_ws(
        {"e": "kline", "E": 1, "s": "BTCUSDT",
         "k": {"t": T0, "T": T0 + 59_999, "s": "BTCUSDT", "i": "1m", "o": "1", "c": "2",
               "h": "3", "l": "0.5", "v": "9", "x": True}},
        "BTCUSDT", 5,
    )  # fmt: skip
    assert c.interval == "1m"
    assert c.closed
    assert c.bar.high == Decimal(3)


async def test_daily_history_paginates_caches_and_updates(tmp_path: Path) -> None:
    total = 2500  # > 2 Seiten à 1000
    calls: list[int] = []

    async def klines(request: web.Request) -> web.Response:
        assert request.query["interval"] == "1d"
        start = int(request.query["startTime"])
        calls.append(start)
        first = max(0, (start - T0) // DAY)
        return web.json_response([row(i) for i in range(first, min(first + 1000, total))])

    app = web.Application()
    app.router.add_get("/api/v3/klines", klines)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        cfg = HistoryConfig(spot_rest_url=f"http://127.0.0.1:{server.port}", start="2017-08-17")
        got: list[CandleHistory] = []

        async def collect(e: CandleHistory) -> None:
            got.append(e)

        async with EventBus() as bus:
            bus.subscribe(CandleHistory, collect)
            hist = BinanceDailyHistory(cfg, ["BTCUSDT"], tmp_path, bus)
            session = aiohttp.ClientSession()
            await hist._update(session, "BTCUSDT")
            await asyncio.sleep(0.05)
            assert len(calls) == 3
            assert len(got[-1].bars) == total
            cache = json.loads((tmp_path / "history/binance_spot/BTCUSDT_1d.json").read_text())
            assert len(cache) == total
            # Zweiter Lauf: nur das Ende wird neu geladen (ab vorletztem Tag)
            calls.clear()
            await hist._update(session, "BTCUSDT")
            await session.close()
            assert calls == [T0 + (total - 3) * DAY]
    finally:
        await server.close()
