"""Bitunix-Live Stufe 2: was genau an die Börse geht – gegen einen Fake-Server, der jede
Signatur nachrechnet und jede Anfrage protokolliert."""

from __future__ import annotations

import json
import socket
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from cockbook.config import LiveConfig
from cockbook.trading.live.bitunix_client import (
    BitunixTradeRest,
    Credentials,
    sign_rest,
    sort_params,
)
from cockbook.trading.live.trader import LiveTrader

CRED = Credentials("myKey", "mySecret")
KEY = "BITUNIX:BTCUSDT"
GRID = (Decimal("0.1"), Decimal("0.0001"))


class Fake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.margin_mode = "ISOLATION"
        self.leverage = 20

    def app(self) -> web.Application:
        async def handler(req: web.Request) -> web.Response:
            h = req.headers
            body = await req.text()
            query = sort_params(dict(req.rel_url.query))
            if h.get("sign") != sign_rest(CRED, h["nonce"], h["timestamp"], query, body):
                return web.json_response({"code": 10007, "msg": "Signature Error"})
            payload = json.loads(body) if body else dict(req.rel_url.query)
            self.calls.append((req.path, payload))
            data: Any = {}
            if req.path.endswith("get_leverage_margin_mode"):
                data = {"symbol": "BTCUSDT", "marginCoin": "USDT", "leverage": self.leverage,
                        "marginMode": self.margin_mode}  # fmt: skip
            elif req.path.endswith("place_order"):
                data = {"orderId": "777", "clientId": payload.get("clientId")}
            elif req.path.endswith("cancel_orders"):
                data = {"successList": [{"orderId": "1"}], "failureList": []}
            return web.json_response({"code": 0, "data": data, "msg": "Success"})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        return app

    def paths(self) -> list[str]:
        return [p.rsplit("/api/v1/futures/", 1)[-1] for p, _ in self.calls]


STATE: dict[str, Any] = {
    "positions": [
        {"symbol": KEY, "position_id": "P1", "qty": 0.5, "mark": 81000.0, "tp": 85000.0,
         "sl": None, "tpsl_ids": ["T1"]},
    ],
    "orders": [
        {"id": "O1", "symbol": KEY, "qty": 0.2, "price": 79000.0, "tp": 83000.0, "sl": None},
    ],
}  # fmt: skip


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[Fake, Any]]:
    fake = Fake()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    runner = web.AppRunner(fake.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    session = aiohttp.ClientSession()

    def make(
        enabled: bool = True,
        state: dict[str, Any] | None = STATE,
        age_s: float = 1.0,
        status: str = "ok",
    ) -> LiveTrader:
        cfg = LiveConfig(trading_enabled=enabled, max_leverage=100)
        rest = BitunixTradeRest(CRED, f"http://127.0.0.1:{port}", session)
        updated = int((time.time() - age_s) * 1000)
        log = tmp_path / "live" / "orders.jsonl"
        grid = lambda k: GRID if k == KEY else None  # noqa: E731
        return LiveTrader(cfg, rest, grid, lambda: state, lambda: (status, updated), log)

    try:
        yield fake, make
    finally:
        await session.close()
        await runner.cleanup()


def req(op: str, **kw: Any) -> dict[str, Any]:
    return {"type": "live", "op": op, "req": 1, **kw}


async def test_disabled_sends_nothing(env: tuple[Fake, Any]) -> None:
    fake, make = env
    r = await make(enabled=False).handle(
        req("order", symbol=KEY, side="buy", kind="market", qty="1")
    )
    assert not r["ok"]
    assert "trading_enabled" in r["message"]
    assert fake.calls == []


async def test_limit_order_is_rounded_and_carries_tpsl(
    env: tuple[Fake, Any], tmp_path: Path
) -> None:
    fake, make = env
    r = await make().handle(req("order", symbol=KEY, side="buy", kind="limit", qty="0.012345",
                                price="80000.04", tp="82000", sl="79500"))  # fmt: skip
    assert r["ok"], r["message"]
    assert fake.paths() == ["account/get_leverage_margin_mode", "trade/place_order"]
    body = fake.calls[-1][1]
    assert body["symbol"] == "BTCUSDT"
    assert body["side"] == "BUY"
    assert body["orderType"] == "LIMIT"
    assert body["qty"] == "0.0123"  # auf Schrittweite ABgerundet
    assert body["price"] == "80000"
    assert body["effect"] == "GTC"
    assert body["reduceOnly"] is False
    assert (body["tpPrice"], body["slPrice"]) == ("82000", "79500")
    assert body["tpStopType"] == body["slStopType"] == "MARK_PRICE"
    assert body["clientId"].startswith("cb")
    assert "Order 777" in r["message"]
    lines = (tmp_path / "live" / "orders.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["ok"] is True
    assert "mySecret" not in lines[-1]


async def test_cross_margin_is_switched_or_blocked(env: tuple[Fake, Any]) -> None:
    fake, make = env
    fake.margin_mode = "CROSS"
    eth = "BITUNIX:BTCUSDT"
    # ohne offene Position/Order -> auf isoliert umstellen, dann Order
    r = await make(state={"positions": [], "orders": []}).handle(
        req("order", symbol=eth, side="sell", kind="market", qty="0.01", ref="81000")
    )
    assert r["ok"], r["message"]
    assert fake.paths() == ["account/get_leverage_margin_mode", "account/change_margin_mode",
                            "trade/place_order"]  # fmt: skip
    assert fake.calls[1][1]["marginMode"] == "ISOLATION"
    # mit offener Position -> nicht umstellbar -> keine Order
    fake.calls.clear()
    r = await make().handle(req("order", symbol=KEY, side="buy", kind="market", qty="0.01"))
    assert not r["ok"]
    assert "isoliert" in r["message"]
    assert fake.paths() == ["account/get_leverage_margin_mode"]


async def test_leverage_above_limit_blocks(env: tuple[Fake, Any]) -> None:
    fake, make = env
    fake.leverage = 125
    r = await make().handle(req("order", symbol=KEY, side="buy", kind="market", qty="0.01"))
    assert not r["ok"]
    assert "max. 100x" in r["message"]
    assert "trade/place_order" not in fake.paths()
    r = await make().handle(req("leverage", symbol=KEY, value=101))
    assert not r["ok"]


async def test_stale_state_blocks_new_orders_but_not_closing(env: tuple[Fake, Any]) -> None:
    fake, make = env
    stale = make(age_s=500)
    r = await stale.handle(req("order", symbol=KEY, side="buy", kind="market", qty="0.01"))
    assert not r["ok"]
    assert "alt" in r["message"]
    assert fake.calls == []
    r = await stale.handle(req("close", symbol=KEY))
    assert r["ok"]
    assert fake.calls[-1] == ("/api/v1/futures/trade/flash_close_position", {"positionId": "P1"})
    broken = make(status="error")
    assert not (await broken.handle(req("order", symbol=KEY, side="buy", kind="market", qty="1")))[
        "ok"
    ]
    assert (await broken.handle(req("cancel", symbol=KEY, id="O1")))["ok"]


async def test_kill_switch_cancels_orders_before_closing(env: tuple[Fake, Any]) -> None:
    fake, make = env
    r = await make(status="error").handle(req("close_all"))
    assert r["ok"]
    assert fake.paths() == ["trade/cancel_all_orders", "trade/close_all_position"]


async def test_unsupported_and_invalid_orders(env: tuple[Fake, Any]) -> None:
    fake, make = env
    t = make()
    r = await t.handle(req("order", symbol=KEY, side="buy", kind="stop", qty="1", trigger="90000"))
    assert "kein Stop" in r["message"]
    r = await t.handle(req("order", symbol="BTCUSDT", side="buy", kind="market", qty="1"))
    assert "nur für Bitunix" in r["message"]
    r = await t.handle(req("order", symbol=KEY, side="buy", kind="limit", qty="0.00001", price="1"))
    assert "zu klein" in r["message"]
    r = await t.handle(req("order", symbol=KEY, side="buy", kind="limit", qty="1", price="80000",
                           tp="79000"))  # fmt: skip
    assert "falschen Seite" in r["message"]
    assert "trade/place_order" not in fake.paths()


async def test_modify_keeps_order_tpsl(env: tuple[Fake, Any]) -> None:
    fake, make = env
    r = await make().handle(req("modify", symbol=KEY, id="O1", price="78500.07"))
    assert r["ok"], r["message"]
    path, body = fake.calls[-1]
    assert path.endswith("trade/modify_order")
    assert body["orderId"] == "O1"
    assert body["qty"] == "0.2"
    assert body["price"] == "78500.1"
    assert body["tpPrice"] == "83000"


async def test_position_tpsl_modify_vs_replace(env: tuple[Fake, Any]) -> None:
    fake, make = env
    t = make()
    # TP bleibt, SL kommt dazu -> ändern
    r = await t.handle(req("tpsl", symbol=KEY, tp="86000", sl="80000"))
    assert r["ok"], r["message"]
    assert fake.paths() == ["tpsl/position/modify_order"]
    assert fake.calls[-1][1]["positionId"] == "P1"
    # TP entfernen -> löschen und nur SL neu setzen
    fake.calls.clear()
    r = await t.handle(req("tpsl", symbol=KEY, tp=None, sl="80000"))
    assert fake.paths() == ["tpsl/cancel_order", "tpsl/position/place_order"]
    assert "tpPrice" not in fake.calls[-1][1]
    # SL über dem Kurs bei Long -> würde sofort auslösen
    r = await t.handle(req("tpsl", symbol=KEY, tp=None, sl="81500"))
    assert "sofort auslösen" in r["message"]


async def test_settings_are_read_without_trading_enabled(env: tuple[Fake, Any]) -> None:
    _, make = env
    r = await make(enabled=False).handle(req("settings", symbol=KEY))
    assert r["ok"]
    assert r["data"] == {"symbol": KEY, "leverage": 20, "margin_mode": "ISOLATION"}
