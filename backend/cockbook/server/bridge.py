"""Lokale WebSocket-Bridge zum Frontend (aiohttp, nur 127.0.0.1).

Kanäle
------
- *Symbol-Kanal*: Heatmap-Spalten, BBO, Trades, Liquidationen eines Symbols, mit
  Historie. Ein Client abonniert genau ein Symbol (das angezeigte Chart).
- *Kennzahlen*: Preis, Funding, OI, Ratios, Liquidationen **aller** Symbole gehen an
  alle Clients – die Seitenleiste zeigt alle Coins gleichzeitig. Pro (Symbol, Art) wird
  der letzte Wert gemerkt und neuen Clients sofort geschickt.

Steuer-Nachrichten vom Frontend (JSON): ``{"type": "subscribe", "symbol": "ETHUSDT"}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque

from aiohttp import WSMsgType, web

from cockbook.config import ServerConfig
from cockbook.server.protocol import encode_control

log = logging.getLogger(__name__)

Frame = str | bytes  # str = JSON-Steuernachricht, bytes = MessagePack


class _Channel:
    def __init__(self, symbol: str, history: int) -> None:
        self.symbol = symbol
        self.hello: str | None = None
        self.status: str | None = None
        # eine Gruppe = alle Frames eines Intervalls
        self.history: deque[list[bytes]] = deque(maxlen=max(history, 1))


class _Client:
    """Eigener Sendepuffer pro Client. Ist er voll, fliegen die ältesten Frames raus:
    ein langsamer Browser-Tab darf weder den Feed noch andere Clients bremsen."""

    def __init__(self, ws: web.WebSocketResponse, maxsize: int) -> None:
        self.ws = ws
        self.symbol: str | None = None
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, frame: Frame) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("slow client, dropping frames", extra={"dropped": self.dropped})
        self.queue.put_nowait(frame)

    def clear(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    async def sender(self) -> None:
        while True:
            frame = await self.queue.get()
            if isinstance(frame, bytes):
                await self.ws.send_bytes(frame)
            else:
                await self.ws.send_str(frame)


class Bridge:
    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self._clients: set[_Client] = set()
        self._channels: dict[str, _Channel] = {}
        self._symbol_info: dict[str, dict[str, str]] = {}
        self._latest_stats: dict[tuple[str, str], bytes] = {}
        self._recent_liqs: deque[bytes] = deque(maxlen=100)
        self._runner: web.AppRunner | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def _channel(self, symbol: str) -> _Channel:
        ch = self._channels.get(symbol)
        if ch is None:
            ch = self._channels[symbol] = _Channel(symbol, self.cfg.history_columns)
        return ch

    def _symbols_msg(self) -> str:
        return encode_control(
            "symbols",
            symbols=[{"symbol": s, **self._symbol_info.get(s, {})} for s in self._channels],
        )

    # --- Daten rein ---------------------------------------------------------------------

    def register_symbol(self, symbol: str, **info: str) -> None:
        """Macht ein Symbol bekannt (Reihenfolge = Reihenfolge in der Seitenleiste)."""
        self._channel(symbol)
        if info:
            self._symbol_info[symbol] = info
        msg = self._symbols_msg()
        for c in self._clients:
            c.put(msg)

    def set_hello(self, symbol: str, hello: str) -> None:
        """Beschreibt Skalierung des Symbols. Ändert sie sich, ist die Historie wertlos."""
        ch = self._channel(symbol)
        if hello != ch.hello:
            ch.history.clear()
            ch.hello = hello
            self._send_channel(symbol, hello)

    def set_status(self, symbol: str, status: str) -> None:
        ch = self._channel(symbol)
        ch.status = status
        self._send_channel(symbol, status)

    def publish(self, symbol: str, frames: list[bytes]) -> None:
        if self.cfg.history_columns:
            self._channel(symbol).history.append(frames)
        for f in frames:
            self._send_channel(symbol, f)

    def publish_stat(self, symbol: str, kind: str, frame: bytes) -> None:
        if kind == "liq":
            self._recent_liqs.append(frame)
        else:
            self._latest_stats[(symbol, kind)] = frame
        for c in self._clients:
            c.put(frame)

    def _send_channel(self, symbol: str, frame: Frame) -> None:
        for c in self._clients:
            if c.symbol == symbol:
                c.put(frame)

    def _send_base_state(self, client: _Client) -> None:
        """Symbolliste + letzte Kennzahlen aller Coins."""
        client.put(self._symbols_msg())
        for frame in self._latest_stats.values():
            client.put(frame)
        for frame in self._recent_liqs:
            client.put(frame)

    def _subscribe(self, client: _Client, symbol: str) -> None:
        ch = self._channels.get(symbol)
        if ch is None:
            client.put(encode_control("error", message=f"unbekanntes Symbol {symbol}"))
            return
        client.symbol = symbol
        # Reihenfolge: hello -> status -> Historie -> live (alles über dieselbe Queue)
        if ch.hello:
            client.put(ch.hello)
        if ch.status:
            client.put(ch.status)
        for group in ch.history:
            for f in group:
                client.put(f)

    # --- Server -------------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        maxsize = self.cfg.client_queue_frames + self.cfg.history_columns * 4 + 1000
        client = _Client(ws, maxsize)
        self._send_base_state(client)
        if self._channels:
            self._subscribe(client, next(iter(self._channels)))
        self._clients.add(client)
        log.info(
            "frontend connected", extra={"peer": request.remote, "clients": len(self._clients)}
        )
        sender = asyncio.create_task(client.sender())
        try:
            async for msg in ws:
                if msg.type is WSMsgType.ERROR:
                    break
                if msg.type is WSMsgType.TEXT:
                    self._on_control(client, msg.data)
        finally:
            self._clients.discard(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionResetError):
                await sender
            log.info("frontend disconnected", extra={"clients": len(self._clients)})
        return ws

    def _on_control(self, client: _Client, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        if msg.get("type") == "subscribe" and isinstance(msg.get("symbol"), str):
            # Alte Chart-Frames verwerfen. Dabei gehen evtl. noch nicht gesendete
            # Grunddaten mit verloren -> neu einreihen (Frontend dedupliziert).
            client.clear()
            self._send_base_state(client)
            self._subscribe(client, msg["symbol"])

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ws", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.cfg.host, self.cfg.port).start()
        log.info("bridge listening", extra={"url": f"ws://{self.cfg.host}:{self.cfg.port}/ws"})

    async def stop(self) -> None:
        for c in list(self._clients):
            await c.ws.close()
        if self._runner:
            await self._runner.cleanup()
