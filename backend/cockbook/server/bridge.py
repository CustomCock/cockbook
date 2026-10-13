"""Lokale WebSocket-Bridge zum Frontend (aiohttp, nur 127.0.0.1)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque

from aiohttp import WSMsgType, web

from cockbook.config import ServerConfig

log = logging.getLogger(__name__)

Frame = str | bytes  # str = JSON-Steuernachricht, bytes = MessagePack


class _Client:
    """Eigener Sendepuffer pro Client. Ist er voll, fliegen die ältesten Frames raus:
    ein langsamer Browser-Tab darf weder den Feed noch andere Clients bremsen."""

    def __init__(self, ws: web.WebSocketResponse, maxsize: int) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, frame: Frame) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("slow client, dropping frames", extra={"dropped": self.dropped})
        self.queue.put_nowait(frame)

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
        # Historie: Liste von Frame-Gruppen (eine Gruppe = ein Intervall)
        self._history: deque[list[bytes]] = deque(maxlen=max(cfg.history_columns, 1))
        self._hello: str | None = None
        self._status: str | None = None
        self._runner: web.AppRunner | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # --- Daten rein ---------------------------------------------------------------------

    def set_hello(self, hello: str) -> None:
        """Beschreibt Symbol/Skalierung. Ändert sie sich, ist die Historie wertlos."""
        if hello != self._hello:
            self._history.clear()
            self._hello = hello
            self._send_all(hello)

    def set_status(self, status: str) -> None:
        self._status = status
        self._send_all(status)

    def publish(self, frames: list[bytes]) -> None:
        if self.cfg.history_columns:
            self._history.append(frames)
        for f in frames:
            self._send_all(f)

    def _send_all(self, frame: Frame) -> None:
        for c in self._clients:
            c.put(frame)

    # --- Server -------------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        client = _Client(ws, self.cfg.client_queue_frames + len(self._history) * 3)
        # Reihenfolge: hello -> status -> Historie -> live (alles über dieselbe Queue)
        if self._hello:
            client.put(self._hello)
        if self._status:
            client.put(self._status)
        for group in self._history:
            for f in group:
                client.put(f)
        self._clients.add(client)
        log.info(
            "frontend connected", extra={"peer": request.remote, "clients": len(self._clients)}
        )
        sender = asyncio.create_task(client.sender())
        try:
            async for msg in ws:
                if msg.type is WSMsgType.ERROR:
                    break
                # Steuer-Nachrichten vom Frontend kommen ab Phase 3/4 (Replay-Steuerung)
        finally:
            self._clients.discard(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionResetError):
                await sender
            log.info("frontend disconnected", extra={"clients": len(self._clients)})
        return ws

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
