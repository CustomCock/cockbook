"""CockBook als MCP-Server: Claude (Desktop/Code) liest Markt und Konto und zeichnet in den Chart.

Start (stdio, so ruft Claude ihn auf):  ``uv run --directory backend python -m cockbook.mcp_server``

Der Server ist ein *zweiter Client* des laufenden CockBook-Backends: Er verbindet sich mit
derselben WebSocket-Bridge wie der Browser (``ws://127.0.0.1:8765/ws?role=agent``) und
benutzt deren Nachrichten. Darum braucht er keine eigenen Börsenverbindungen, und alles,
was Claude zeichnet, erscheint sofort in jedem offenen Chart.

Was Claude darf – bewusst begrenzt:
- lesen: Symbole, Orderbuch-Zusammenfassung, Trade-Flow, Kerzen, Kennzahlen,
  Paper-Konto, echtes Konto (nur Anzeige)
- zeichnen: Zeichnungen anlegen/ändern/löschen (markiert als "von Claude")
- Paper-Trading: Orders, Schließen, Löschen (simuliert, kein echtes Geld)
- **keine echten Orders.** Dieses Modul schickt nie eine ``live``-Nachricht; ``_send``
  verweigert sie zusätzlich. Echte Orders bleiben dem Browser mit Freischaltung und
  Bestätigung vorbehalten.

Wichtig für stdio: nie auf stdout schreiben – stdout ist der Protokoll-Kanal. Logs gehen
auf stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import aiohttp
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from cockbook.server.drawings import EXPANDED_POINTS, TOOL_POINTS, drawing_key

log = logging.getLogger("cockbook.mcp")

DEFAULT_URL = "ws://127.0.0.1:8765/ws"
CONNECT_TIMEOUT_S = 5.0
REPLY_TIMEOUT_S = 10.0
# Diese Nachrichtentypen darf der MCP-Server nie senden
_FORBIDDEN = frozenset({"live"})
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class BackendError(ToolError):
    """Fehler, dessen Text Claude sehen soll (ToolError wird nicht durch "Error executing
    tool" ersetzt) – z. B. Backend nicht erreichbar, Zeichnung abgelehnt."""


class InputError(ToolError, ValueError):
    """Ungültige Eingabe von Claude (Werkzeug, Zeit, Farbe) – Meldung geht an Claude zurück."""


class BridgeClient:
    """Eine WebSocket-Verbindung zum Backend; Antworten werden per ``req`` zugeordnet."""

    def __init__(self, url: str, token: str | None) -> None:
        self.url = url
        self.token = token
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._req = 0
        self._waiting: dict[int, tuple[set[str], asyncio.Future[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        # Zustand, den das Backend jedem Client schickt (Paper-/Live-Konto, Symbolliste)
        self.shared: dict[str, dict[str, Any]] = {}

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        async with self._lock:
            if self._ws is not None and not self._ws.closed:
                return self._ws
            if self._session is None:
                self._session = aiohttp.ClientSession()
            params = {"role": "agent"}
            if self.token:
                params["token"] = self.token
            try:
                ws = await self._session.ws_connect(
                    self.url,
                    params=params,
                    timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT_S),
                    heartbeat=20,
                    max_msg_size=32 << 20,
                )
            except (aiohttp.ClientError, OSError, TimeoutError) as e:
                raise BackendError(
                    f"CockBook-Backend nicht erreichbar ({self.url}). Läuft CockBook "
                    "(start.sh / start.bat bzw. `uv run python -m cockbook`)?"
                ) from e
            self._ws = ws
            self._reader = asyncio.create_task(self._read(ws))
            return ws

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                self._dispatch(data)
        finally:
            # Verbindung weg: wartende Anfragen beenden, nächster Aufruf verbindet neu
            for _, fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(BackendError("Verbindung zum Backend getrennt"))
            self._waiting.clear()
            if ws.close_code == 4401:
                self.shared["auth_error"] = {"type": "auth_error"}

    def _dispatch(self, data: dict[str, Any]) -> None:
        typ = data.get("type")
        if typ in ("paper_state", "live_state", "symbols"):
            self.shared[typ] = data
        req = data.get("req")
        if isinstance(req, int) and req in self._waiting:
            types, fut = self._waiting[req]
            if typ in types and not fut.done():
                del self._waiting[req]
                fut.set_result(data)

    async def request(self, msg: dict[str, Any], reply_types: set[str]) -> dict[str, Any]:
        if msg.get("type") in _FORBIDDEN:
            raise BackendError("echte Orders sind über Claude gesperrt")
        ws = await self._connect()
        self._req += 1
        req = self._req
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[req] = (reply_types, fut)
        await ws.send_str(json.dumps({**msg, "req": req}))
        try:
            return await asyncio.wait_for(fut, REPLY_TIMEOUT_S)
        except TimeoutError as e:
            self._waiting.pop(req, None)
            if "auth_error" in self.shared:
                raise BackendError("Backend verlangt ein Token: COCKBOOK_TOKEN setzen") from e
            raise BackendError("Backend antwortet nicht") from e

    async def state(self, typ: str) -> dict[str, Any] | None:
        """Gemeinsamen Zustand holen (kommt direkt nach dem Verbinden)."""
        await self._connect()
        for _ in range(20):
            if typ in self.shared:
                return self.shared[typ]
            await asyncio.sleep(0.1)
        return None

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._session is not None:
            await self._session.close()


# --- Hilfen -----------------------------------------------------------------------------------


def parse_time(value: str | float) -> int:
    """Zeit -> ms UTC. Erlaubt: Unix-ms, Unix-Sekunden, ISO-8601 ("2026-10-18T14:30:00Z"),
    "now" und relativ "-15m" / "-2h" / "-1d" (vor jetzt) bzw. "+30m"."""
    now = int(time.time() * 1000)
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v * 1000) if v < 1e11 else int(v)  # Sekunden vs. Millisekunden
    s = value.strip()
    if s == "now":
        return now
    m = re.fullmatch(r"([+-])(\d+(?:\.\d+)?)([smhd])", s)
    if m:
        mult = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[m.group(3)]
        delta = int(float(m.group(2)) * mult)
        return now + delta if m.group(1) == "+" else now - delta
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise InputError(f"Zeit nicht lesbar: {value!r} (ISO-8601, Unix-ms oder -15m)") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def build_drawing(
    tool: str,
    points: list[dict[str, Any]],
    *,
    color: str | None = None,
    text: str | None = None,
    width: float | None = None,
    dash: str | None = None,
    fill: str | None = None,
    levels: list[float] | None = None,
    extend_left: bool | None = None,
    extend_right: bool | None = None,
    drawing_id: str | None = None,
) -> dict[str, Any]:
    """Zeichnung im Format des Frontends (Werte-Prüfung macht das Backend)."""
    if tool not in TOOL_POINTS:
        raise InputError(f"unbekanntes Werkzeug {tool!r} – siehe list_drawing_tools")
    need = EXPANDED_POINTS.get(tool)
    if need is not None and len(points) != need:
        raise InputError(f"{tool} braucht {need} Punkte (siehe list_drawing_tools)")
    pts = []
    for p in points:
        if "price" not in p or "time" not in p:
            raise InputError("jeder Punkt braucht time und price")
        pts.append({"t": parse_time(p["time"]), "p": float(p["price"])})
    style: dict[str, Any] = {}
    if color is not None:
        if not _HEX_COLOR.fullmatch(color):
            raise InputError("color als #rrggbb angeben")
        style["color"] = color
    for k, v in (
        ("text", text),
        ("width", width),
        ("dash", dash),
        ("fill", fill),
        ("levels", levels),
        ("extendLeft", extend_left),
        ("extendRight", extend_right),
    ):
        if v is not None:
            style[k] = v
    if tool in ("ray",) and extend_right is None:
        style["extendRight"] = True
    if tool == "extended":
        style.setdefault("extendLeft", True)
        style.setdefault("extendRight", True)
    return {
        "id": drawing_id or f"c{int(time.time() * 1000):x}{secrets.token_hex(3)}",
        "tool": tool,
        "pts": pts,
        "style": style,
        "by": "claude",
        "created": int(time.time() * 1000),
    }


TOOL_HELP: dict[str, str] = {
    "hline": "horizontale Linie (Unterstützung/Widerstand) – 1 Punkt, nur price zählt",
    "hray": "horizontaler Strahl ab einem Zeitpunkt nach rechts – 1 Punkt",
    "vline": "vertikale Linie (Ereignis) – 1 Punkt, nur time zählt",
    "trend": "Trendlinie – 2 Punkte",
    "ray": "Strahl (nach rechts verlängert) – 2 Punkte",
    "extended": "Linie in beide Richtungen verlängert – 2 Punkte",
    "parallel": "paralleler Kanal – 3 Punkte (Linie A-B, Punkt 3 = Kanalbreite)",
    "fibret": "Fibonacci-Retracement – 2 Punkte (Start, Ende der Bewegung), levels optional",
    "fibext": "trendbasierte Fib-Extension – 3 Punkte",
    "rect": "Rechteck/Zone (z. B. Liquiditätszone) – 2 Punkte (gegenüberliegende Ecken)",
    "long": "Long-Position – 3 Punkte: Einstieg, Ziel (TP), Stop (SL); time von Punkt 2 = Ende",
    "short": "Short-Position – 3 Punkte: Einstieg, Ziel (TP), Stop (SL)",
    "pricerange": "Preisbereich messen – 2 Punkte",
    "dateprice": "Zeit- und Preisbereich messen – 2 Punkte",
    "fixedvp": "Volumenprofil eines festen Zeitbereichs – 2 Punkte",
    "avwap": "Anchored VWAP ab Zeitpunkt – 1 Punkt",
    "text": "Text – 1 Punkt, text angeben",
    "note": "Notiz mit Hintergrund – 1 Punkt, text angeben",
    "callout": "Sprechblase mit Zeiger – 2 Punkte (Ziel, Blase), text angeben",
    "pricelabel": "Preis-Label – 1 Punkt",
    "arrowup": "Pfeil nach oben (Kaufsignal) – 1 Punkt",
    "arrowdown": "Pfeil nach unten (Verkaufssignal) – 1 Punkt",
    "arrow": "Pfeil von A nach B – 2 Punkte",
    "polyline": "Polylinie – beliebig viele Punkte (min. 2)",
}


# --- MCP-Server -------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
    try:
        yield
    finally:
        if _client is not None:
            await _client.close()  # WebSocket + HTTP-Sitzung sauber schließen


mcp = MCPServer(
    name="cockbook",
    lifespan=_lifespan,
    instructions=(
        "CockBook ist eine lokale Orderflow-/Liquiditäts-Heatmap für Krypto (Binance, Bitunix). "
        "Symbole: 'BTCUSDT' = Binance, 'BITUNIX:BTCUSDT' = Bitunix, 'MULTI:BTCUSDT' = beide "
        "kombiniert. Für Analysen: get_orderbook (Wände, Ungleichgewicht), get_trade_flow "
        "(Delta, große Trades), get_candles. Zeichnungen gelten je Coin für alle Börsen und "
        "erscheinen sofort im Chart des Nutzers; zeichne sparsam und beschrifte sie. Zeiten "
        "sind UTC. Echte Orders sind über diese Schnittstelle nicht möglich; Paper-Orders "
        "sind simuliert. Keine Anlageberatung."
    ),
)
_client: BridgeClient | None = None


def client() -> BridgeClient:
    global _client
    if _client is None:
        _client = BridgeClient(
            os.environ.get("COCKBOOK_URL", DEFAULT_URL), os.environ.get("COCKBOOK_TOKEN") or None
        )
    return _client


async def _query(what: str, **params: Any) -> Any:
    reply = await client().request({"type": "query", "what": what, **params}, {"query_result"})
    if not reply.get("ok"):
        raise BackendError(str(reply.get("message", "Abfrage fehlgeschlagen")))
    return reply.get("data")


async def _draw(op: str, symbol: str, **params: Any) -> dict[str, Any]:
    key = drawing_key(symbol)
    reply = await client().request(
        {"type": "draw", "op": op, "key": key, **params}, {"drawings", "drawings_error"}
    )
    if reply.get("type") == "drawings_error":
        raise BackendError(str(reply.get("message")))
    return reply


async def _paper(op: str, **params: Any) -> str:
    reply = await client().request({"type": "paper", "op": op, **params}, {"paper_result"})
    msg = str(reply.get("message", ""))
    if not reply.get("ok"):
        raise BackendError(f"Paper-Order abgelehnt: {msg}")
    return msg


READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
DRAW = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
# Paper-Orders: simuliert, aber der Nutzer soll jede einzelne sehen und bestätigen
PAPER = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
CONFIRM = {"anthropic/requiresUserInteraction": True}

Symbol = Annotated[
    str, Field(description="z. B. BTCUSDT (Binance), BITUNIX:BTCUSDT, MULTI:BTCUSDT")
]


@mcp.tool(annotations=READ)
async def list_symbols() -> list[dict[str, Any]]:
    """Alle Symbole mit Orderbuch im Backend: synchron?, Tick-Größe, Bid/Ask."""
    result: list[dict[str, Any]] = await _query("symbols")
    return result


@mcp.tool(annotations=READ)
async def get_orderbook(
    symbol: Symbol,
    range_pct: Annotated[
        float | None,
        Field(description="Bereich ± % um den Mid (Standard aus config.yaml, meist 1)"),
    ] = None,
    walls: Annotated[int | None, Field(description="Anzahl größter Level je Seite")] = None,
    group: Annotated[
        float | None, Field(description="Preise zu Stufen bündeln, z. B. 10 = je 10 USDT")
    ] = None,
) -> dict[str, Any]:
    """Orderbuch-Zusammenfassung: Best Bid/Ask, Spread, Summen je Seite im Bereich,
    Ungleichgewicht (+1 nur Bids … -1 nur Asks) und die größten Wände mit Abstand zum Mid."""
    params: dict[str, Any] = {"symbol": symbol}
    for k, v in (("range_pct", range_pct), ("walls", walls), ("group", group)):
        if v is not None:
            params[k] = v
    result: dict[str, Any] = await _query("book", **params)
    return result


@mcp.tool(annotations=READ)
async def get_trade_flow(
    symbol: Symbol,
    seconds: Annotated[float, Field(description="Zeitfenster in Sekunden (max. 86400)")] = 300,
) -> dict[str, Any]:
    """Aggressive Käufe vs. Verkäufe im Zeitfenster: Volumen, Delta (je Börse), VWAP, größte Trades.
    covered_seconds zeigt, wie weit der Trade-Puffer tatsächlich zurückreicht."""
    result: dict[str, Any] = await _query("trades", symbol=symbol, seconds=seconds)
    return result


@mcp.tool(annotations=READ)
async def get_candles(
    symbol: Symbol,
    minutes: Annotated[
        Literal[1, 3, 5, 15, 30, 60, 240], Field(description="Kerzen-Intervall")
    ] = 5,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    """OHLCV-Kerzen (Zeit in ms UTC), neueste zuletzt. Basis: 1m-Kerzen der Börse (~1 Tag)."""
    result: dict[str, Any] = await _query("candles", symbol=symbol, minutes=minutes, limit=limit)
    return result


@mcp.tool(annotations=READ)
async def get_market_stats(symbol: Symbol) -> dict[str, Any]:
    """Letzte Kennzahlen je Börse: 24h-Ticker, Mark-Preis und Funding, Open Interest, Long/Short."""
    result: dict[str, Any] = await _query("stats", symbol=symbol)
    return result


@mcp.tool(annotations=READ)
async def get_paper_account() -> dict[str, Any]:
    """Paper-Trading-Konto (simuliert): Kontostand, offene Positionen, Orders."""
    st = await client().state("paper_state")
    if st is None:
        raise BackendError("Paper-Trading ist im Backend aus (trading.paper.enabled)")
    return {k: v for k, v in st.items() if k != "type"}


@mcp.tool(annotations=READ)
async def get_live_account() -> dict[str, Any]:
    """Echtes Bitunix-Konto, nur lesend: Status, Guthaben, Positionen, offene Orders.
    Handeln ist über Claude nicht möglich."""
    st = await client().state("live_state")
    if st is None:
        raise BackendError("kein Live-Konto verbunden (BITUNIX_API_KEY/SECRET nicht gesetzt?)")
    return {k: v for k, v in st.items() if k != "type"}


@mcp.tool(annotations=READ)
async def list_drawing_tools() -> list[dict[str, Any]]:
    """Alle Zeichenwerkzeuge (wie in der Werkzeugleiste) mit Anzahl der Ankerpunkte."""
    out = []
    for tool, pts in TOOL_POINTS.items():
        n = EXPANDED_POINTS.get(tool, pts)
        row: dict[str, Any] = {"tool": tool, "points": n}
        if tool in TOOL_HELP:
            row["hint"] = TOOL_HELP[tool]
        out.append(row)
    return out


@mcp.tool(annotations=READ)
async def list_drawings(symbol: Symbol) -> list[dict[str, Any]]:
    """Zeichnungen des Coins (Zeiten in ms UTC). by = "user" oder "claude"."""
    reply = await _draw("load", symbol)
    items: list[dict[str, Any]] = reply.get("items", [])
    return items


Point = Annotated[
    dict[str, Any],
    Field(
        description='{"time": "2026-10-18T14:30:00Z" | Unix-ms | "-30m" | "now", "price": 67250.5}'
    ),
]


@mcp.tool(annotations=DRAW)
async def add_drawing(
    symbol: Symbol,
    tool: Annotated[
        str,
        Field(
            description="Werkzeug-ID, z. B. hline, trend, rect, fibret, long (list_drawing_tools)"
        ),
    ],
    points: Annotated[list[Point], Field(description="Ankerpunkte in Reihenfolge")],
    text: Annotated[
        str | None, Field(description="Beschriftung (bei text/note/callout Pflicht)")
    ] = None,
    color: Annotated[str | None, Field(description="#rrggbb")] = None,
    width: Annotated[float | None, Field(ge=0.5, le=12)] = None,
    dash: Literal["solid", "dashed", "dotted"] | None = None,
    fill: Annotated[str | None, Field(description="Füllfarbe, z. B. rgba(90,204,255,0.15)")] = None,
    levels: Annotated[
        list[float] | None, Field(description="Fib-/Gann-Stufen, z. B. [0,0.382,0.5,0.618,1]")
    ] = None,
    extend_left: bool | None = None,
    extend_right: bool | None = None,
) -> dict[str, Any]:
    """Zeichnung in den Chart setzen (erscheint sofort beim Nutzer, markiert "von Claude").
    Gilt für den Coin auf allen Börsen. Rückgabe: id zum späteren Ändern/Löschen."""
    d = build_drawing(
        tool,
        points,
        color=color,
        text=text,
        width=width,
        dash=dash,
        fill=fill,
        levels=levels,
        extend_left=extend_left,
        extend_right=extend_right,
    )
    await _draw("put", symbol, item=d)
    return {"id": d["id"], "key": drawing_key(symbol), "tool": tool, "points": len(d["pts"])}


@mcp.tool(annotations=DRAW)
async def update_drawing(
    symbol: Symbol,
    drawing_id: str,
    points: list[Point] | None = None,
    text: str | None = None,
    color: str | None = None,
    hidden: bool | None = None,
) -> dict[str, Any]:
    """Bestehende Zeichnung ändern (Punkte, Text, Farbe, ausblenden)."""
    items = (await _draw("load", symbol)).get("items", [])
    d = next((x for x in items if x.get("id") == drawing_id), None)
    if d is None:
        raise BackendError(f"Zeichnung {drawing_id} nicht gefunden")
    if points is not None:
        d["pts"] = build_drawing(d["tool"], points)["pts"]
    if text is not None:
        d["style"]["text"] = text
    if color is not None:
        if not _HEX_COLOR.fullmatch(color):
            raise InputError("color als #rrggbb angeben")
        d["style"]["color"] = color
    if hidden is not None:
        d["hidden"] = hidden
    await _draw("put", symbol, item=d)
    return {"id": drawing_id, "updated": True}


@mcp.tool(annotations=DELETE)
async def remove_drawing(symbol: Symbol, drawing_id: str) -> dict[str, Any]:
    """Eine Zeichnung löschen."""
    await _draw("delete", symbol, id=drawing_id)
    return {"id": drawing_id, "removed": True}


@mcp.tool(annotations=DELETE)
async def clear_drawings(
    symbol: Symbol,
    only_claude: Annotated[
        bool, Field(description="true = nur Claudes eigene Zeichnungen (Standard)")
    ] = True,
) -> dict[str, Any]:
    """Zeichnungen des Coins löschen – standardmäßig nur die von Claude."""
    reply = await _draw("clear", symbol, **({"by": "claude"} if only_claude else {}))
    return {"remaining": len(reply.get("items", []))}


@mcp.tool(annotations=PAPER, meta=CONFIRM)
async def place_paper_order(
    symbol: Annotated[
        str, Field(description="Börsen-Symbol, z. B. BTCUSDT oder BITUNIX:BTCUSDT (nicht MULTI)")
    ],
    side: Literal["buy", "sell"],
    qty: Annotated[float, Field(gt=0, description="Menge in Basis-Währung (z. B. BTC)")],
    kind: Literal["market", "limit", "stop"] = "market",
    price: Annotated[float | None, Field(description="Limit-Preis (bei limit)")] = None,
    trigger: Annotated[float | None, Field(description="Auslösepreis (bei stop)")] = None,
    tp: float | None = None,
    sl: float | None = None,
    reduce_only: bool = False,
) -> str:
    """Paper-Order (simuliert gegen das echte Orderbuch, kein echtes Geld)."""
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "kind": kind,
        "qty": str(qty),
        "reduce_only": reduce_only,
    }
    for k, v in (("price", price), ("trigger", trigger), ("tp", tp), ("sl", sl)):
        if v is not None:
            params[k] = str(v)
    return await _paper("order", **params)


@mcp.tool(annotations=PAPER, meta=CONFIRM)
async def close_paper_position(symbol: str) -> str:
    """Paper-Position eines Symbols zum Marktpreis schließen."""
    return await _paper("close", symbol=symbol)


@mcp.tool(annotations=PAPER)
async def cancel_paper_orders(symbol: str | None = None) -> str:
    """Offene Paper-Orders löschen (eines Symbols oder alle)."""
    return await _paper("cancel_all", **({"symbol": symbol} if symbol else {}))


def main() -> None:
    # stdout gehört dem MCP-Protokoll -> Logs nur auf stderr
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
