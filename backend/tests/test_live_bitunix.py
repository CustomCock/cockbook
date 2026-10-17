"""Bitunix-Live (Stufe 1, nur lesen): Signatur, Credentials, Abgleich gegen Fake-Server."""

from __future__ import annotations

import json
import socket
from typing import Any

import aiohttp
from aiohttp import web

from cockbook.config import LiveConfig, ServerConfig
from cockbook.server.bridge import Bridge
from cockbook.trading.live.bitunix_client import (
    BitunixPrivateRest,
    Credentials,
    sign_rest,
    sign_ws,
    sort_params,
    ws_login_message,
)
from cockbook.trading.live.service import LiveAccountService, build_state

CRED = Credentials("myKey", "mySecret")


def test_signatures_match_official_demo() -> None:
    # Erwartete Werte erzeugt mit BitunixOfficial/open-api Demo/Python/*_sign.py
    q = sort_params({"symbol": "BTCUSDT", "marginCoin": "USDT"})
    assert q == "marginCoinUSDTsymbolBTCUSDT"
    assert (
        sign_rest(CRED, "abc123", "1700000000000", q, "")
        == (
            "0fa25a56020f60585ba8f26788bee630317e9f7e6cf6d674c8529e14309c1b49"  # pragma: allowlist secret
        )
    )
    body = '{"symbol":"BTCUSDT","qty":"0.01"}'
    assert (
        sign_rest(CRED, "abc123", "1700000000000", "", body)
        == (
            "4af8c3230f423a834e1ff8e77339e284e716a99f3f2ae4f4e6962ab953d0a4e2"  # pragma: allowlist secret
        )
    )
    assert (
        sign_ws(CRED, "abc123", "1700000000")
        == (
            "6dd0fd4d2e4736931afc302526685b35d4208f7e7d00cc667471d9fbb69e91b1"  # pragma: allowlist secret
        )
    )


def test_credentials_never_show_secret() -> None:
    c = Credentials("abcdefghijklmnop1234", "TOPSECRET")
    assert "TOPSECRET" not in repr(c)
    assert "abcdefghijkl" not in repr(c)
    assert c.masked_key == "…1234"
    assert Credentials.from_env({}) is None
    assert Credentials.from_env({"BITUNIX_API_KEY": "k", "BITUNIX_API_SECRET": " "}) is None
    got = Credentials.from_env({"BITUNIX_API_KEY": " k ", "BITUNIX_API_SECRET": "s"})
    assert got == Credentials("k", "s")


def test_ws_login_message() -> None:
    msg = json.loads(ws_login_message(CRED, now_s=1700000000))
    assert msg["op"] == "login"
    args = msg["args"][0]
    assert args["apiKey"] == "myKey"  # pragma: allowlist secret
    assert args["timestamp"] == 1700000000
    assert args["sign"] == sign_ws(CRED, args["nonce"], "1700000000")
    assert "mySecret" not in json.dumps(msg)


# --- Fake-Bitunix (Beispielantworten aus der Doku) ------------------------------------

ACCOUNT = [{"marginCoin": "USDT", "available": "1000", "frozen": "5", "margin": "10",
            "transfer": "1000", "positionMode": "HEDGE", "crossUnrealizedPNL": "2",
            "isolationUnrealizedPNL": "1.5", "bonus": "0"}]  # fmt: skip
POSITIONS = [
    {"positionId": "12345678", "symbol": "BTCUSDT", "qty": "0.5", "side": "SHORT",
     "positionMode": "ONE_WAY", "marginMode": "ISOLATION", "leverage": 100, "fee": "0.1",
     "funding": "-0.2", "realizedPNL": "0", "margin": "300", "unrealizedPNL": "1.5",
     "liqPrice": "62209", "avgOpenPrice": "60000", "ctime": 1691382137448},
]  # fmt: skip
ORDERS = {"orderList": [{"orderId": "11111", "qty": "1", "tradeQty": "0.5", "price": "59000",
                         "symbol": "BTCUSDT", "side": "BUY", "orderType": "LIMIT",
                         "status": "NEW", "reduceOnly": True, "ctime": 1597026383085}],
          "total": 1}  # fmt: skip
TPSL = [{"id": "123", "positionId": "12345678", "symbol": "BTCUSDT", "tpPrice": "55000",
         "slPrice": "61000"}]  # fmt: skip


def fake_bitunix(cred: Credentials, *, fail_code: int | None = None) -> web.Application:
    async def handler(req: web.Request) -> web.Response:
        h = req.headers
        query = sort_params(dict(req.rel_url.query))
        body = await req.text()
        if h.get("api-key") != cred.api_key or h.get("sign") != sign_rest(
            cred, h["nonce"], h["timestamp"], query, body
        ):
            return web.json_response({"code": 10007, "msg": "Signature Error"})
        if fail_code is not None:
            return web.json_response({"code": fail_code, "msg": "IP not in whitelist"})
        data: Any = {
            "/api/v1/futures/account": ACCOUNT,
            "/api/v1/futures/account/position_mode": {"positionMode": "HEDGE"},
            "/api/v1/futures/position/get_pending_positions": POSITIONS,
            "/api/v1/futures/trade/get_pending_orders": ORDERS,
            "/api/v1/futures/tpsl/get_pending_orders": TPSL,
        }[req.path]
        return web.json_response({"code": 0, "data": data, "msg": "Success"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, f"http://127.0.0.1:{port}"


async def test_refresh_reads_account_positions_orders() -> None:
    runner, url = await _serve(fake_bitunix(CRED))
    bridge = Bridge(ServerConfig())
    svc = LiveAccountService(LiveConfig(rest_url=url), {}, bridge, CRED)
    try:
        async with aiohttp.ClientSession() as session:
            await svc.refresh(BitunixPrivateRest(CRED, url, session))
    finally:
        await runner.cleanup()
    assert svc.status == "ok"
    state = json.loads(bridge._shared["live"])
    assert state["read_only"] is True
    assert state["key"] == "…"  # kurzer Test-Key: nichts davon zeigen
    acc = state["account"]
    assert acc["equity"] == 1000 + 5 + 10 + 1.5 + 2
    pos = state["positions"][0]
    assert pos["symbol"] == "BITUNIX:BTCUSDT"
    assert pos["qty"] == -0.5  # SHORT -> negativ
    assert (pos["tp"], pos["sl"]) == (55000, 61000)
    assert pos["liq"] == 62209
    order = state["orders"][0]
    assert (order["side"], order["kind"], order["reduce_only"]) == ("buy", "limit", True)
    assert any("One-Way" in w for w in state["warnings"])  # Konto steht auf HEDGE
    assert "mySecret" not in bridge._shared["live"]


async def test_wrong_secret_and_whitelist_errors_are_explained() -> None:
    runner, url = await _serve(fake_bitunix(CRED))
    bridge = Bridge(ServerConfig())
    wrong = Credentials("myKey", "falsch")
    svc = LiveAccountService(LiveConfig(rest_url=url), {}, bridge, wrong)
    try:
        async with aiohttp.ClientSession() as session:
            await svc.refresh(BitunixPrivateRest(wrong, url, session))
    finally:
        await runner.cleanup()
    assert svc.status == "error"
    assert "Key/Secret prüfen" in svc.message
    runner, url = await _serve(fake_bitunix(CRED, fail_code=10006))
    try:
        async with aiohttp.ClientSession() as session:
            await svc.refresh(BitunixPrivateRest(CRED, url, session))
    finally:
        await runner.cleanup()
    assert "Whitelist" in svc.message


def test_build_state_without_positions() -> None:
    s = build_state(ACCOUNT[0], "ONE_WAY", [], {"orderList": []}["orderList"], [])
    assert s["warnings"] == []
    assert s["positions"] == []


async def test_no_keys_publishes_status_and_returns() -> None:
    import asyncio

    bridge = Bridge(ServerConfig())
    svc = LiveAccountService(LiveConfig(), {}, bridge, None)
    await asyncio.wait_for(svc.run(asyncio.Event()), 1)
    assert json.loads(bridge._shared["live"])["status"] == "no_keys"


async def test_ws_login_rejection_is_detected() -> None:
    bridge = Bridge(ServerConfig())
    svc = LiveAccountService(LiveConfig(), {}, bridge, CRED)
    await svc._on_message('{"op":"connect","data":{"result":true}}', 0)
    assert svc.status != "error"
    # echte Antwort von Bitunix bei falschem Key (2026-10-17)
    await svc._on_message(
        '{"op":"login","data":{"msg":"invalid params，Authorize: Token invalid",'  # noqa: RUF001 – Originaltext
        '"nonce":"bf6f76152ce5848936598b1c9527992e","result":false}}',  # pragma: allowlist secret
        0,
    )
    assert svc.status == "error"
    assert "Key/Secret prüfen" in svc.message
    ok = LiveAccountService(LiveConfig(), {}, bridge, CRED)
    await ok._on_message('{"op":"login","data":{"result":true}}', 0)
    assert ok.status == "connecting"
