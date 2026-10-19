"""Lokale WebSocket-Bridge zum Frontend (aiohttp, nur 127.0.0.1).

Kanäle
------
- *Symbol-Kanal*: Heatmap-Spalten, BBO, Trades, Liquidationen eines Symbols, mit
  Historie. Ein Client abonniert genau ein Symbol (das angezeigte Chart).
- *Kennzahlen*: Preis, Funding, OI, Ratios, Liquidationen **aller** Symbole gehen an
  alle Clients – die Seitenleiste zeigt alle Coins gleichzeitig. Pro (Symbol, Art) wird
  der letzte Wert gemerkt und neuen Clients sofort geschickt.

Steuer-Nachrichten vom Frontend (JSON):
``{"type": "subscribe", "symbol": "ETHUSDT", "since": ms, "since_long": ms}``.
``since`` (optional): Das Frontend hat die Spalten bis zu diesem Zeitpunkt noch im Speicher
(Tab-Wechsel) – dann kommt nur der fehlende Rest der Historie.
``{"type": "long", "symbol": "BTCUSDT", "tier": "1D"}``: Langzeit-Ansicht öffnen (Archiv +
Kerzen, danach neue Spalten dieser Stufe live); ``{"type": "long_klines", …}`` nur Kerzen
neu; ``{"type": "long_off"}`` beendet das Langzeit-Abo.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

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
        # Zustand, den jeder neue Abonnent einmal bekommt (z. B. 1m-Kerzen-Historie)
        self.static: dict[str, bytes] = {}
        # eine Gruppe = alle Frames eines Intervalls (Zeitstempel in ms, Schlüsselbild?)
        self.history: deque[tuple[int, list[bytes], bool]] = deque(maxlen=max(history, 1))


class _Client:
    """Eigener Sendepuffer pro Client. Ist er voll, fliegen die ältesten Frames raus:
    ein langsamer Browser-Tab darf weder den Feed noch andere Clients bremsen."""

    def __init__(self, ws: web.WebSocketResponse, maxsize: int, *, text_only: bool = False) -> None:
        self.ws = ws
        # Agent (Claude/MCP): nur Steuer-Nachrichten, keine Chart-/Kennzahl-Frames
        self.text_only = text_only
        self.symbol: str | None = None
        self.long_sub: tuple[str, str] | None = None  # (Symbol, Stufe) der Langzeit-Ansicht
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, frame: Frame) -> None:
        if self.text_only and isinstance(frame, bytes):
            return
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
    def __init__(self, cfg: ServerConfig, *, token: str | None = None) -> None:
        """``token``: Server-Betrieb (nicht nur localhost) – jeder Client muss ihn als
        ``?token=…`` mitschicken. Kommt aus einer Umgebungsvariable, nie aus Dateien."""
        self.cfg = cfg
        self._token = token
        self._clients: set[_Client] = set()
        self._channels: dict[str, _Channel] = {}
        self._symbol_info: dict[str, dict[str, str]] = {}
        self._latest_stats: dict[tuple[str, str], bytes] = {}
        # Gesamtmarkt: bei Last mehrere pro Sekunde -> etwas mehr vorhalten
        self._recent_liqs: deque[bytes] = deque(maxlen=300)
        self._runner: web.AppRunner | None = None
        # Reihenfolge der Symbole (Config) und Hook beim Abo (Orderbuch bei Bedarf starten)
        self._order: list[str] = []
        self.on_subscribe: Callable[[str], None] | None = None
        # Liefert die Frames einer Langzeit-Anfrage (Symbol, Stufe, "all" | "klines")
        self.long_handler: Callable[[str, str, str], Awaitable[list[Frame]]] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # Paper-Trading: Anfrage -> Antwort an den Absender
        self.paper_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        # Echtes Konto: Anfrage -> Antwort (asynchron, geht an die Börse)
        self.live_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        # Zeichnungen: Anfrage -> (Antwort, an alle senden?)
        self.draw_handler: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]] | None = None
        # Abfragen (Claude/MCP): Orderbuch-Zusammenfassung, Trades, Kerzen …
        self.query_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        # KI-Panel: Anfrage + Rückkanal zum anfragenden Tab (Antworten kommen gestreamt)
        self.ai_handler: (
            Callable[[Callable[[dict[str, Any]], None], dict[str, Any]], Awaitable[None]] | None
        ) = None
        # Zustand für alle Clients (z. B. Paper-Konto), auch für neu verbundene
        self._shared: dict[str, str] = {}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def _channel(self, symbol: str) -> _Channel:
        ch = self._channels.get(symbol)
        if ch is None:
            ch = self._channels[symbol] = _Channel(symbol, self.cfg.history_columns)
        return ch

    def set_symbol_order(self, symbols: list[str]) -> None:
        self._order = list(symbols)

    def _sorted_symbols(self) -> list[str]:
        rank = {s: i for i, s in enumerate(self._order)}
        return sorted(self._channels, key=lambda s: rank.get(s, len(rank)))

    def _symbols_msg(self) -> str:
        return encode_control(
            "symbols",
            symbols=[{"symbol": s, **self._symbol_info.get(s, {})} for s in self._sorted_symbols()],
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

    def publish(
        self, symbol: str, frames: list[bytes], ts_ms: int = 0, *, keyframe: bool = True
    ) -> None:
        """``keyframe=False``: Die Spalte enthält nur Änderungen zur vorigen – ein Client
        braucht vorher das letzte Schlüsselbild (siehe ``_subscribe``)."""
        if self.cfg.history_columns:
            self._channel(symbol).history.append((ts_ms, frames, keyframe))
        for f in frames:
            self._send_channel(symbol, f)

    def publish_tier(self, symbol: str, tier: str, frame: bytes) -> None:
        """Neue Spalte einer Langzeit-Stufe an alle, die sie gerade ansehen."""
        key = (symbol, tier)
        for c in self._clients:
            if c.long_sub == key:
                c.put(frame)

    def set_shared(self, key: str, text: str) -> None:
        """Zustand, den alle Clients haben sollen (neue bekommen ihn beim Verbinden)."""
        self._shared[key] = text
        self.broadcast(text)

    def broadcast(self, frame: Frame) -> None:
        for c in self._clients:
            c.put(frame)

    def watches_tier(self, symbol: str, tier: str) -> bool:
        """Sieht gerade jemand diese Langzeit-Stufe an? (vorläufige Spalten nur dann bauen)"""
        return any(c.long_sub == (symbol, tier) for c in self._clients)

    def set_static(self, symbol: str, key: str, frame: bytes) -> None:
        """Ersetzt einen Zustand des Symbols und schickt ihn an aktuelle Abonnenten."""
        self._channel(symbol).static[key] = frame
        self._send_channel(symbol, frame)

    def send_live(self, symbol: str, frame: bytes) -> None:
        """Nur an aktuelle Abonnenten, ohne Historie (z. B. laufende Kerze)."""
        self._send_channel(symbol, frame)

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
        for text in self._shared.values():
            client.put(text)
        for frame in self._latest_stats.values():
            client.put(frame)
        for frame in self._recent_liqs:
            client.put(frame)

    def _subscribe(
        self,
        client: _Client,
        symbol: str,
        since: int | None = None,
    ) -> None:
        if self.on_subscribe is not None:
            self.on_subscribe(symbol)
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
        for frame in ch.static.values():
            client.put(frame)
        # Ab dem letzten Schlüsselbild <= since (bzw. dem ältesten): Änderungs-Spalten ohne
        # ihr Schlüsselbild wären wertlos. Spalten <= since verwirft das Frontend als schon
        # vorhanden, baut daraus aber seinen Bucket-Zustand wieder auf.
        hist = list(ch.history)
        start = next((i for i, (_, _, key) in enumerate(hist) if key), len(hist))
        if since is not None:
            for i in range(start, len(hist)):
                if hist[i][0] > since:
                    break
                if hist[i][2]:
                    start = i
        for _, group, _ in hist[start:]:
            for f in group:
                client.put(f)

    # --- Server -------------------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        """Server-Betrieb: Token prüfen (zeitkonstanter Vergleich, sonst verrät die
        Antwortzeit, wie viele Zeichen schon stimmen) und nur Seiten derselben Adresse
        zulassen – eine fremde Webseite im selben Browser soll sich nicht verbinden können."""
        if self._token is None:
            return True
        given = request.query.get("token", "")
        if not hmac.compare_digest(given.encode(), self._token.encode()):
            return False
        origin = request.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == request.host

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        if not self._authorized(request):
            # Eigener Close-Code, damit das Frontend nach dem Token fragen kann
            log.warning("frontend rejected (token/origin)", extra={"peer": request.remote})
            await ws.send_str(encode_control("auth_error"))
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        maxsize = (
            self.cfg.client_queue_frames
            + self.cfg.history_columns * 4
            + self.cfg.long_queue_frames
            + 1000
        )
        # ?role=agent: MCP-Erweiterung – fragt nur ab, braucht keinen Chart-Datenstrom
        agent = request.query.get("role") == "agent"
        client = _Client(ws, maxsize, text_only=agent)
        self._send_base_state(client)
        if self._channels and not agent:
            self._subscribe(client, self._sorted_symbols()[0])
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
            since = msg.get("since")
            self._subscribe(client, msg["symbol"], since if isinstance(since, int) else None)
        elif msg.get("type") in ("long", "long_klines"):
            sym, tier = msg.get("symbol"), msg.get("tier")
            if isinstance(sym, str) and isinstance(tier, str):
                if msg["type"] == "long":
                    client.long_sub = (sym, tier)
                    if self.on_subscribe is not None:
                        self.on_subscribe(sym)  # Orderbuch starten -> ab jetzt Daten sammeln
                self._spawn(
                    self._long(client, sym, tier, "all" if msg["type"] == "long" else "klines")
                )
        elif msg.get("type") == "long_off":
            client.long_sub = None
        elif msg.get("type") == "paper" and self.paper_handler is not None:
            try:
                reply = self.paper_handler(msg)
            except Exception:
                # Programmfehler: Verbindung nicht abreißen lassen, Nutzer informieren
                log.exception("paper request failed", extra={"op": msg.get("op")})
                reply = {"type": "paper_result", "req": msg.get("req"), "ok": False,
                         "message": "interner Fehler – siehe Backend-Log"}  # fmt: skip
            client.put(json.dumps(reply))
        elif msg.get("type") == "live" and self.live_handler is not None:
            self._spawn(self._live(client, msg))
        elif msg.get("type") == "draw" and self.draw_handler is not None:
            try:
                reply, everyone = self.draw_handler(msg)
            except Exception:
                log.exception("draw request failed", extra={"op": msg.get("op")})
                reply = {"type": "drawings_error", "key": msg.get("key"), "req": msg.get("req"),
                         "message": "interner Fehler – siehe Backend-Log"}  # fmt: skip
                everyone = False
            text = json.dumps(reply)
            if everyone:
                self.broadcast(text)
            else:
                client.put(text)
        elif msg.get("type") == "ai" and self.ai_handler is not None and not client.text_only:
            # Agenten (MCP) dürfen kein weiteres KI-Programm starten – keine Rekursion
            self._spawn(self._ai(client, msg))
        elif msg.get("type") == "query" and self.query_handler is not None:
            try:
                reply = self.query_handler(msg)
            except Exception:
                log.exception("query failed", extra={"what": msg.get("what")})
                reply = {"type": "query_result", "req": msg.get("req"), "ok": False,
                         "message": "interner Fehler – siehe Backend-Log"}  # fmt: skip
            client.put(json.dumps(reply))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _live(self, client: _Client, msg: dict[str, Any]) -> None:
        assert self.live_handler is not None
        try:
            reply = await self.live_handler(msg)
        except Exception:
            log.exception("live request failed", extra={"op": msg.get("op")})
            reply = {"type": "live_result", "req": msg.get("req"), "ok": False,
                     "message": "interner Fehler – siehe Backend-Log", "data": {}}  # fmt: skip
        if client in self._clients:
            client.put(json.dumps(reply))

    async def _ai(self, client: _Client, msg: dict[str, Any]) -> None:
        assert self.ai_handler is not None

        def send(obj: dict[str, Any]) -> None:
            if client in self._clients:
                client.put(json.dumps(obj))

        try:
            await self.ai_handler(send, msg)
        except Exception:
            log.exception("ai request failed", extra={"op": msg.get("op")})
            for ev in (
                {"kind": "error", "text": "interner Fehler – siehe Backend-Log"},
                {"kind": "done"},
            ):
                send({"type": "ai_event", "chat": msg.get("chat"), "event": ev})

    async def _long(self, client: _Client, sym: str, tier: str, what: str) -> None:
        if self.long_handler is None:
            return
        try:
            frames = await self.long_handler(sym, tier, what)
        except Exception:
            log.exception("long request failed", extra={"symbol": sym, "tier": tier})
            frames = [encode_control("error", message="Langzeit-Daten nicht verfügbar")]
        # Inzwischen umgeschaltet? Dann nicht mehr senden
        if client.long_sub == (sym, tier) and client in self._clients:
            for f in frames:
                client.put(f)

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
