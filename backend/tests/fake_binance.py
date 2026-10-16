"""Minimaler Fake-Binance-Server (REST + WebSocket) für Integrationstests.

Jede WebSocket-Verbindung spielt ein Skript ab. Skript-Elemente:
- ``dict``: wird als Combined-Stream-Nachricht gesendet
- ``("wait_depth_calls", n)``: warten, bis der REST-Depth-Endpoint n-mal bedient wurde
- ``("close",)``: Verbindung serverseitig schließen
Nach Skriptende bleibt die Verbindung offen.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

FIX = Path(__file__).parent / "fixtures" / "binance"

ScriptItem = dict[str, Any] | tuple[Any, ...]


def depth_msg(first: int, final: int, prev: int, bids: Any = (), asks: Any = ()) -> dict[str, Any]:
    return {
        "stream": "btcusdt@depth@100ms",
        "data": {
            "e": "depthUpdate",
            "E": 1_700_000_000_000 + final,
            "T": 1_700_000_000_000 + final,
            "s": "BTCUSDT",
            "U": first,
            "u": final,
            "pu": prev,
            "b": [list(x) for x in bids],
            "a": [list(x) for x in asks],
        },
    }


def snapshot(last_id: int, bids: Any, asks: Any) -> dict[str, Any]:
    return {
        "lastUpdateId": last_id,
        "E": 1,
        "T": 1,
        "bids": [list(x) for x in bids],
        "asks": [list(x) for x in asks],
    }


class FakeBinance:
    def __init__(self) -> None:
        self.exchange_info = json.loads((FIX / "exchange_info.json").read_text())
        self.snapshots: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.depth_calls = 0
        self.public_scripts: list[list[ScriptItem]] = []
        self.market_scripts: list[list[ScriptItem]] = []
        self.public_connections = 0
        self.market_connections = 0
        self.liquidation_script: list[ScriptItem] = []
        self._server: TestServer | None = None

    @property
    def base(self) -> str:
        assert self._server is not None
        return f"127.0.0.1:{self._server.port}"

    async def __aenter__(self) -> FakeBinance:
        app = web.Application()
        app.router.add_get("/fapi/v1/exchangeInfo", self._exchange_info)
        app.router.add_get("/fapi/v1/depth", self._depth)
        app.router.add_get("/public/stream", self._public)
        app.router.add_get("/market/stream", self._market)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        await self._server.close()

    async def _exchange_info(self, request: web.Request) -> web.Response:
        return web.json_response(self.exchange_info)

    async def _depth(self, request: web.Request) -> web.Response:
        assert request.query["symbol"] == "BTCUSDT"
        snap = await self.snapshots.get()
        self.depth_calls += 1
        return web.json_response(snap)

    async def _run_script(
        self, request: web.Request, script: list[ScriptItem]
    ) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for item in script:
            if isinstance(item, dict):
                await ws.send_str(json.dumps(item))
                await asyncio.sleep(0.01)
            elif item[0] == "wait_depth_calls":
                while self.depth_calls < item[1]:
                    await asyncio.sleep(0.005)
                await asyncio.sleep(0.05)
            elif item[0] == "close":
                await ws.close()
                return ws
        async for msg in ws:  # offen halten, bis der Client trennt
            if msg.type is WSMsgType.ERROR:
                break
        return ws

    async def _public(self, request: web.Request) -> web.WebSocketResponse:
        assert "btcusdt@depth" in request.query["streams"]
        i = self.public_connections
        self.public_connections += 1
        script = self.public_scripts[i] if i < len(self.public_scripts) else []
        return await self._run_script(request, script)

    async def _market(self, request: web.Request) -> web.WebSocketResponse:
        streams = request.query["streams"].split("/")
        if streams == ["!forceOrder@arr"]:
            return await self._run_script(request, self.liquidation_script)
        assert "btcusdt@aggTrade" in streams
        assert "btcusdt@forceOrder" in streams
        i = self.market_connections
        self.market_connections += 1
        script = self.market_scripts[i] if i < len(self.market_scripts) else []
        return await self._run_script(request, script)
