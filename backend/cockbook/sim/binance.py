"""Binance-USDⓈ-M-Simulator – NUR für interne Tests ohne Binance-Zugang.

Gedacht für Entwicklungsumgebungen, aus denen Binance nicht erreichbar ist (z. B.
Cloud-Container mit HTTP 451). Abnahmen und echte Nutzung laufen immer mit
Binance-Livedaten. Start::

    python -m cockbook.sim.binance --port 9876
    python -m cockbook --config backend/cockbook/sim/config.sim.yaml

Simuliert ein *konsistentes* Buch: interne Änderungen bekommen nicht fortlaufende
Update-IDs (wie bei Binance), werden alle ``interval_ms`` zu einem Diff-Event
(U/u/pu) gebündelt, und REST-Snapshots spiegeln den exakten Zustand bei ihrer
``lastUpdateId`` – die oft *mitten* im nächsten Event liegt. Das ist genau der Fall,
den die Sync-Prozedur (U <= lastUpdateId <= u) abdecken muss.

Kein Ersatz für echte Börsendaten – Marktmikrostruktur ist hier Zufall.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import random
import time
from decimal import Decimal
from typing import Any

from aiohttp import web

log = logging.getLogger("cockbook.sim")

TICK = Decimal("0.10")
SYMBOL = "BTCUSDT"


class SimBook:
    def __init__(self, mid: int = 650_000, depth: int = 1500, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.mid = mid  # Asks ab mid, Bids bis mid-1
        self.depth = depth
        self.bids: dict[int, str] = {}
        self.asks: dict[int, str] = {}
        self.update_id = 1_000_000
        for k in range(depth):
            if self.rng.random() < 0.6:
                self.bids[mid - 1 - k] = self._qty()
            if self.rng.random() < 0.6:
                self.asks[mid + k] = self._qty()
        self.bids.setdefault(mid - 1, self._qty())
        self.asks.setdefault(mid, self._qty())
        self._pending: dict[str, dict[int, str]] = {"b": {}, "a": {}}
        self._first_pending: int | None = None
        self.last_emitted = self.update_id

    def _qty(self) -> str:
        return f"{self.rng.expovariate(1 / 2):.3f}"

    def _set(self, side: str, price: int, qty: str) -> None:
        self.update_id += self.rng.randint(1, 40)  # IDs nicht fortlaufend
        if self._first_pending is None:
            self._first_pending = self.update_id
        book = self.bids if side == "b" else self.asks
        if qty == "0":
            book.pop(price, None)
        else:
            book[price] = qty
        self._pending[side][price] = qty

    def step(self) -> None:
        """Eine interne Änderung."""
        r = self.rng.random()
        if r < 0.03:
            self._move_mid(self.rng.choice((-1, 1)))
            return
        k = min(int(self.rng.expovariate(1 / 30)), self.depth - 1)
        if self.rng.random() < 0.5:
            price, side = self.mid - 1 - k, "b"
            book = self.bids
        else:
            price, side = self.mid + k, "a"
            book = self.asks
        qty = "0" if (price in book and self.rng.random() < 0.3) else self._qty()
        if k == 0 and qty == "0":
            qty = self._qty()  # Top-of-Book nie leer lassen
        self._set(side, price, qty)

    def _move_mid(self, direction: int) -> None:
        if direction > 0:
            # bisheriges bestes Ask-Level wird zum Bid-Bereich
            self._set("a", self.mid, "0")
            self.mid += 1
            self._set("b", self.mid - 1, self._qty())
            if self.mid not in self.asks:
                self._set("a", self.mid, self._qty())
        else:
            self._set("b", self.mid - 1, "0")
            self.mid -= 1
            self._set("a", self.mid, self._qty())
            if self.mid - 1 not in self.bids:
                self._set("b", self.mid - 1, self._qty())

    def take_event(self) -> dict[str, Any] | None:
        if self._first_pending is None:
            return None
        now = int(time.time() * 1000)
        ev = {
            "e": "depthUpdate",
            "E": now,
            "T": now,
            "s": SYMBOL,
            "U": self._first_pending,
            "u": self.update_id,
            "pu": self.last_emitted,
            "b": [[str(p * TICK), q] for p, q in self._pending["b"].items()],
            "a": [[str(p * TICK), q] for p, q in self._pending["a"].items()],
        }
        self.last_emitted = self.update_id
        self._pending = {"b": {}, "a": {}}
        self._first_pending = None
        return ev

    def snapshot(self, limit: int) -> dict[str, Any]:
        bids = sorted(self.bids.items(), reverse=True)[:limit]
        asks = sorted(self.asks.items())[:limit]
        now = int(time.time() * 1000)
        return {
            "lastUpdateId": self.update_id,
            "E": now,
            "T": now,
            "bids": [[str(p * TICK), q] for p, q in bids],
            "asks": [[str(p * TICK), q] for p, q in asks],
        }


class Simulator:
    def __init__(self, interval_ms: int = 100, changes_per_event: int = 20) -> None:
        self.book = SimBook()
        self.interval = interval_ms / 1000
        self.changes = changes_per_event
        self.public: set[web.WebSocketResponse] = set()
        self.market: set[web.WebSocketResponse] = set()
        self.trade_id = 1

    async def tick_loop(self) -> None:
        while True:
            n = self.book.rng.randint(1, self.changes * 2)
            # Änderungen über das Intervall verteilt, damit REST-Snapshots mitten
            # in ein Event fallen können.
            for _ in range(n):
                self.book.step()
                await asyncio.sleep(self.interval / n)
            ev = self.book.take_event()
            if ev:
                msg = json.dumps({"stream": "btcusdt@depth@100ms", "data": ev})
                for ws in list(self.public):
                    await ws.send_str(msg)
            if self.book.rng.random() < 0.5:
                await self._trade()

    async def _trade(self) -> None:
        buy = self.book.rng.random() < 0.5
        price = self.book.mid if buy else self.book.mid - 1
        now = int(time.time() * 1000)
        data = {
            "e": "aggTrade",
            "E": now,
            "a": self.trade_id,
            "s": SYMBOL,
            "p": str(price * TICK),
            "q": f"{self.book.rng.expovariate(1 / 0.3):.3f}",
            "f": self.trade_id,
            "l": self.trade_id,
            "T": now,
            "m": not buy,
        }
        self.trade_id += 1
        msg = json.dumps({"stream": "btcusdt@aggTrade", "data": data})
        for ws in list(self.market):
            await ws.send_str(msg)

    async def exchange_info(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "symbols": [
                    {
                        "symbol": SYMBOL,
                        "status": "TRADING",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": str(TICK)},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        ],
                    }
                ]
            }
        )

    async def depth(self, request: web.Request) -> web.Response:
        await asyncio.sleep(self.book.rng.uniform(0.05, 0.4))  # Netzwerk-Latenz
        snap = self.book.snapshot(int(request.query.get("limit", "500")))
        await asyncio.sleep(self.book.rng.uniform(0.05, 0.4))
        return web.json_response(snap)

    async def _ws(self, request: web.Request, pool: set[web.WebSocketResponse]) -> Any:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        pool.add(ws)
        try:
            async for _ in ws:
                pass
        finally:
            pool.discard(ws)
        return ws

    async def ws_public(self, request: web.Request) -> Any:
        return await self._ws(request, self.public)

    async def ws_market(self, request: web.Request) -> Any:
        return await self._ws(request, self.market)


async def serve(port: int) -> None:
    sim = Simulator()
    app = web.Application()
    app.router.add_get("/fapi/v1/exchangeInfo", sim.exchange_info)
    app.router.add_get("/fapi/v1/depth", sim.depth)
    app.router.add_get("/public/stream", sim.ws_public)
    app.router.add_get("/market/stream", sim.ws_market)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("simulator listening on 127.0.0.1:%d", port)
    try:
        await sim.tick_loop()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(prog="cockbook.sim.binance")
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.port))


if __name__ == "__main__":
    main()
