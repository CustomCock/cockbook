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
import math
import random
import time
from decimal import Decimal
from typing import Any

from aiohttp import web

log = logging.getLogger("cockbook.sim")

# Symbol -> (Tick-Größe, Start-Mid in Ticks, typische Trade-Größe)
SYMBOLS: dict[str, tuple[Decimal, int, float]] = {
    "BTCUSDT": (Decimal("0.10"), 650_000, 0.3),
    "ETHUSDT": (Decimal("0.01"), 300_000, 3.0),
    "SOLUSDT": (Decimal("0.01"), 15_000, 40.0),
    "XRPUSDT": (Decimal("0.0001"), 25_000, 3000.0),
    "BNBUSDT": (Decimal("0.01"), 60_000, 5.0),
    "DOGEUSDT": (Decimal("0.00001"), 20_000, 20000.0),
    "XAUUSDT": (Decimal("0.01"), 400_000, 2.0),
    "XAGUSDT": (Decimal("0.001"), 48_000, 50.0),
    "TSLAUSDT": (Decimal("0.01"), 42_000, 20.0),
    "NVDAUSDT": (Decimal("0.01"), 18_000, 40.0),
    "EWJUSDT": (Decimal("0.01"), 7_500, 100.0),
}
# TradFi-Perps: kein Spot-Markt (Tageskerzen nur über /fapi), contractType laut Doku;
# Wert = underlyingType
TRADFI = {
    "XAUUSDT": "COMMODITY",
    "XAGUSDT": "COMMODITY",
    "TSLAUSDT": "EQUITY",
    "NVDAUSDT": "EQUITY",
    "EWJUSDT": "EQUITY",
}


class SimBook:
    def __init__(
        self,
        symbol: str = "BTCUSDT",
        tick: Decimal = Decimal("0.10"),
        mid: int = 650_000,
        depth: int = 1500,
        seed: int | None = None,
    ) -> None:
        self.symbol = symbol
        self.tick = tick
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
            "s": self.symbol,
            "U": self._first_pending,
            "u": self.update_id,
            "pu": self.last_emitted,
            "b": [[str(p * self.tick), q] for p, q in self._pending["b"].items()],
            "a": [[str(p * self.tick), q] for p, q in self._pending["a"].items()],
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
            "bids": [[str(p * self.tick), q] for p, q in bids],
            "asks": [[str(p * self.tick), q] for p, q in asks],
        }


class Simulator:
    def __init__(self, interval_ms: int = 100, changes_per_event: int = 20) -> None:
        self.books = {
            sym: SimBook(sym, tick, mid, seed=n)
            for n, (sym, (tick, mid, _)) in enumerate(SYMBOLS.items())
        }
        self.rng = random.Random(42)
        self.interval = interval_ms / 1000
        self.changes = changes_per_event
        # Verbindung -> abonnierte Symbole (aus ?streams=…)
        self.public: dict[web.WebSocketResponse, set[str]] = {}
        self.market: dict[web.WebSocketResponse, set[str]] = {}
        self.trade_id = 1
        self.daily: dict[str, list[list[Any]]] = {}
        # laufende 1m-Kerze je Symbol: [open_ms, o, h, l, c, v]
        self.candles: dict[str, list[Any]] = {}
        self.oi = {
            sym: 50_000.0 / (mid * float(tick)) * 1000 for sym, (tick, mid, _) in SYMBOLS.items()
        }

    async def _send(
        self, pool: dict[web.WebSocketResponse, set[str]], sym: str, stream: str, data: Any
    ) -> None:
        msg = json.dumps({"stream": stream, "data": data})
        for ws, syms in list(pool.items()):
            if sym in syms:
                await ws.send_str(msg)

    async def tick_loop(self) -> None:
        last_stats = 0.0
        while True:
            n = self.rng.randint(1, self.changes * 2)
            # Änderungen über das Intervall verteilt, damit REST-Snapshots mitten
            # in ein Event fallen können.
            for _ in range(n):
                for book in self.books.values():
                    book.step()
                await asyncio.sleep(self.interval / n)
            for sym, book in self.books.items():
                ev = book.take_event()
                if ev:
                    await self._send(self.public, sym, f"{sym.lower()}@depth@100ms", ev)
                if self.rng.random() < 0.5:
                    await self._trade(sym, book)
                if self.rng.random() < 0.01:
                    await self._liquidation(sym, book)
                if self.rng.random() < 0.4:
                    await self._send_kline(sym, int(time.time() * 1000))
            if self.rng.random() < 0.3:
                await self._foreign_liquidation()
            if time.monotonic() - last_stats >= 1.0:
                last_stats = time.monotonic()
                await self._stats()
                if self.rng.random() < 0.1:
                    await self._session()

    def _candle_update(self, sym: str, price: Decimal, qty: float, now: int) -> None:
        start = now // 60_000 * 60_000
        c = self.candles.get(sym)
        if c is None or c[0] != start:
            c = self.candles[sym] = [start, price, price, price, price, 0.0]
        c[2] = max(c[2], price)
        c[3] = min(c[3], price)
        c[4] = price
        c[5] += qty

    async def _send_kline(self, sym: str, now: int) -> None:
        c = self.candles.get(sym)
        if c is None:
            return
        closed = now // 60_000 * 60_000 != c[0]
        data = {
            "e": "kline", "E": now, "s": sym,
            "k": {"t": c[0], "T": c[0] + 59_999, "s": sym, "i": "1m", "o": str(c[1]),
                  "c": str(c[4]), "h": str(c[2]), "l": str(c[3]), "v": f"{c[5]:.3f}", "x": closed},
        }  # fmt: skip
        await self._send(self.market, sym, f"{sym.lower()}@kline_1m", data)
        if closed:
            del self.candles[sym]

    async def _trade(self, sym: str, book: SimBook) -> None:
        buy = self.rng.random() < 0.5
        price = book.mid if buy else book.mid - 1
        now = int(time.time() * 1000)
        data = {
            "e": "aggTrade",
            "E": now,
            "a": self.trade_id,
            "s": sym,
            "p": str(price * book.tick),
            "q": f"{self.rng.expovariate(1 / SYMBOLS[sym][2]):.3f}",
            "f": self.trade_id,
            "l": self.trade_id,
            "T": now,
            "m": not buy,
        }
        self.trade_id += 1
        self._candle_update(sym, price * book.tick, float(str(data["q"])), now)
        await self._send(self.market, sym, f"{sym.lower()}@aggTrade", data)

    async def _liquidation(self, sym: str, book: SimBook) -> None:
        long_liq = self.rng.random() < 0.5
        price = str((book.mid - 3 if long_liq else book.mid + 3) * book.tick)
        qty = f"{self.rng.expovariate(1 / (SYMBOLS[sym][2] * 5)):.3f}"
        now = int(time.time() * 1000)
        data = {
            "e": "forceOrder",
            "E": now,
            "o": {
                "s": sym,
                "S": "SELL" if long_liq else "BUY",
                "o": "LIMIT",
                "f": "IOC",
                "q": qty,
                "p": price,
                "ap": price,
                "X": "FILLED",
                "l": qty,
                "z": qty,
                "T": now,
            },
        }
        await self._send(self.market, sym, f"{sym.lower()}@forceOrder", data)
        await self._send(self.market, "*ALL", "!forceOrder@arr", {**data, "st": 1})

    async def _foreign_liquidation(self) -> None:
        """Liquidationen anderer Coins für den Gesamtmarkt-Stream."""
        sym, price = self.rng.choice([("DOGEUSDT", 0.18), ("BNBUSDT", 600.0), ("ADAUSDT", 0.45)])
        now = int(time.time() * 1000)
        qty = f"{self.rng.expovariate(1 / (20_000 / price)):.1f}"
        o = {"s": sym, "S": self.rng.choice(["BUY", "SELL"]), "o": "LIMIT", "f": "IOC",
             "q": qty, "p": str(price), "ap": str(price), "X": "FILLED", "l": qty, "z": qty,
             "T": now}  # fmt: skip
        await self._send(
            self.market, "*ALL", "!forceOrder@arr", {"e": "forceOrder", "E": now, "o": o, "st": 1}
        )

    async def _stats(self) -> None:
        """Gesamtmarkt-Streams wie ``!markPrice@arr@1s`` / ``!miniTicker@arr`` (Arrays)."""
        marks, tickers = [], []
        for sym, book in self.books.items():
            m, t = self._stat_items(sym, book)
            marks.append(m)
            tickers.append(t)
            await self._send(self.market, sym, f"{sym.lower()}@markPrice@1s", m)
            await self._send(self.market, sym, f"{sym.lower()}@miniTicker", t)
        # Coin-margined Eintrag, den CockBook verwerfen muss
        marks.append({**marks[0], "s": "BTCUSD_PERP", "st": 2})
        await self._send(self.market, "*ALL", "!markPrice@arr@1s", marks)
        await self._send(self.market, "*ALL", "!miniTicker@arr", tickers)

    async def _session(self) -> None:
        now = int(time.time() * 1000)
        hour = now // 3_600_000 * 3_600_000
        for etype, session in (
            ("CommodityUpdate", "REGULAR" if (now // 3_600_000) % 6 else "NO_TRADING"),
            ("EquityUpdate", "OVERNIGHT"),
        ):
            await self._send(
                self.market,
                "*ALL",
                "tradingSession",
                {"e": etype, "E": now, "t": hour, "T": hour + 3_600_000, "S": session},
            )

    def _stat_items(self, sym: str, book: SimBook) -> tuple[dict[str, Any], dict[str, Any]]:
        now = int(time.time() * 1000)
        mid = book.mid * book.tick
        mark = {
            "e": "markPriceUpdate",
            "E": now,
            "s": sym,
            "p": str(mid),
            "i": str(mid),
            "P": str(mid),
            "r": f"{self.rng.uniform(-0.0002, 0.0003):.8f}",
            "T": (now // 28_800_000 + 1) * 28_800_000,
        }
        o = mid * Decimal("0.98")
        ticker = {
            "e": "24hrMiniTicker",
            "E": now,
            "s": sym,
            "c": str(mid),
            "o": str(o),
            "h": str(mid * Decimal("1.01")),
            "l": str(o * Decimal("0.99")),
            "v": "123456.7",
            "q": "987654321.0",
        }
        return mark, ticker

    async def exchange_info(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "symbols": [
                    {
                        "symbol": sym,
                        "status": "TRADING",
                        "contractType": "TRADIFI_PERPETUAL" if sym in TRADFI else "PERPETUAL",
                        "underlyingType": TRADFI.get(sym, "COIN"),
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": str(tick)},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        ],
                    }
                    for sym, (tick, _, _) in {
                        **SYMBOLS,
                        "ADAUSDT": (Decimal("0.0001"), 0, 0.0),
                    }.items()
                ]
            }
        )

    async def depth(self, request: web.Request) -> web.Response:
        book = self.books[request.query["symbol"]]
        await asyncio.sleep(self.rng.uniform(0.05, 0.4))  # Netzwerk-Latenz
        snap = book.snapshot(int(request.query.get("limit", "500")))
        await asyncio.sleep(self.rng.uniform(0.05, 0.4))
        return web.json_response(snap)

    async def open_interest(self, request: web.Request) -> web.Response:
        sym = request.query["symbol"]
        self.oi[sym] *= self.rng.uniform(0.998, 1.002)
        return web.json_response(
            {"openInterest": f"{self.oi[sym]:.3f}", "symbol": sym, "time": int(time.time() * 1000)}
        )

    async def long_short(self, request: web.Request) -> web.Response:
        lo = self.rng.uniform(0.4, 0.7)
        return web.json_response(
            [
                {
                    "symbol": request.query["symbol"],
                    "longShortRatio": f"{lo / (1 - lo):.4f}",
                    "longAccount": f"{lo:.4f}",
                    "shortAccount": f"{1 - lo:.4f}",
                    "timestamp": int(time.time() * 1000),
                }
            ]
        )

    async def taker(self, request: web.Request) -> web.Response:
        b, s = self.rng.uniform(100, 500), self.rng.uniform(100, 500)
        return web.json_response(
            [
                {
                    "buySellRatio": f"{b / s:.4f}",
                    "buyVol": f"{b:.4f}",
                    "sellVol": f"{s:.4f}",
                    "timestamp": int(time.time() * 1000),
                }
            ]
        )

    async def klines_1m(self, request: web.Request) -> web.Response:
        sym = request.query["symbol"]
        if request.query.get("interval") == "1d":
            return await self.klines_1d(request)
        book = self.books[sym]
        limit = int(request.query.get("limit", "500"))
        now_min = int(time.time() * 1000) // 60_000 * 60_000
        rows = []
        price = float(book.mid * book.tick)
        for i in range(limit):
            t = now_min - i * 60_000
            o = price * (1 + self.rng.gauss(0, 0.0008))
            h, low = max(o, price) * 1.0004, min(o, price) * 0.9996
            rows.append(
                [t, f"{o:.6f}", f"{h:.6f}", f"{low:.6f}", f"{price:.6f}", "12.3", t + 59_999]
            )
            price = o
        return web.json_response(rows[::-1])

    async def klines_1d_spot(self, request: web.Request) -> web.Response:
        if request.query["symbol"] in TRADFI:
            return web.json_response({"code": -1121, "msg": "Invalid symbol."}, status=400)
        return await self.klines_1d(request)

    async def klines_1d(self, request: web.Request) -> web.Response:
        """Synthetische Tageshistorie ab 2017 (deterministisch je Symbol)."""
        sym = request.query["symbol"]
        start = int(request.query["startTime"])
        limit = int(request.query.get("limit", "500"))
        if sym not in self.daily:
            rng = random.Random(sym)
            day0 = 1_502_928_000_000
            days = (int(time.time() * 1000) - day0) // 86_400_000 + 1
            end = float(self.books[sym].mid * self.books[sym].tick)
            closes = [end]
            for _ in range(days - 1):
                closes.append(closes[-1] / math.exp(rng.gauss(0.0007, 0.035)))
            closes.reverse()
            self.daily[sym] = [
                [day0 + i * 86_400_000, f"{c * 0.99:.6f}", f"{c * 1.03:.6f}", f"{c * 0.97:.6f}",
                 f"{c:.6f}", "1000", day0 + (i + 1) * 86_400_000 - 1]
                for i, c in enumerate(closes)
            ]  # fmt: skip
        rows = [r for r in self.daily[sym] if r[0] >= start][:limit]
        return web.json_response(rows)

    async def _ws(self, request: web.Request, pool: dict[web.WebSocketResponse, set[str]]) -> Any:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        syms = {
            "*ALL" if st.startswith("!") or st == "tradingSession" else st.split("@")[0].upper()
            for st in request.query.get("streams", "").split("/")
        }
        pool[ws] = syms
        try:
            async for _ in ws:
                pass
        finally:
            pool.pop(ws, None)
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
    app.router.add_get("/fapi/v1/openInterest", sim.open_interest)
    app.router.add_get("/fapi/v1/klines", sim.klines_1m)
    app.router.add_get("/api/v3/klines", sim.klines_1d_spot)
    app.router.add_get("/futures/data/globalLongShortAccountRatio", sim.long_short)
    app.router.add_get("/futures/data/takerlongshortRatio", sim.taker)
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
