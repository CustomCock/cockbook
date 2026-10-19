"""Bitunix-Futures (USDT-M): Marktdaten für Multibook.

Doku (gelesen am 2026-10-17):
- Einführung/Domains: https://www.bitunix.com/api-docs/futures/common/introduction.html
- WebSocket (Limits, Ping): https://www.bitunix.com/api-docs/futures/websocket/prepare/WebSocket.html
- Depth-Kanal: https://www.bitunix.com/api-docs/futures/websocket/public/depth%20channel.html
- Trades: https://www.bitunix.com/api-docs/futures/websocket/public/Trade%20Channel.html
- Tickers: https://www.bitunix.com/api-docs/futures/websocket/public/Tickers%20Channel.html
- Handelspaare: https://www.bitunix.com/api-docs/futures/market/get_trading_pairs.html
- Kerzen: https://www.bitunix.com/api-docs/futures/market/get_kline.html
  (max. 200 je Abfrage, 10 Abfragen/s, nur abgeschlossene Kerzen, neueste zuerst)

Orderbuch-Prozedur
------------------
Bitunix liefert keine Sequenznummern. Dafür enthält *jede* ``depth_books``-Nachricht das
vollständige Buch (gemessen: BTC ~25 000 Level von -90 % bis weit über den Kurs, ~3 Nachrichten
pro Sekunde). Lücken im eigentlichen Sinn gibt es damit nicht: Jede Nachricht ersetzt den Stand.
Damit der Rest von CockBook nicht 25 000 Level dreimal pro Sekunde verarbeiten muss, vergleicht
``BitunixBookDiffer`` jede Nachricht mit der vorigen (auf den Rohstrings, ohne Umrechnung) und
gibt nur die Änderungen als ``BookDelta`` weiter (gemessen ~30 je Nachricht). Die erste Nachricht
nach jedem (Re-)Connect wird zum ``BookSnapshot``.

Limits: max. 5 Nachrichten/s vom Client (auch Pings, sonst Trennung/IP-Sperre), max. 300 Abos je
Verbindung. Ping per JSON ``{"op": "ping", "ping": <Sekunden>}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from decimal import Decimal
from typing import Any

import aiohttp

from cockbook.adapters.common import (
    RestError,
    bars_to_fetch,
    get_json,
    run_ws,
    sleep_or_stop,
    sleep_to_minute,
)
from cockbook.config import BitunixConfig
from cockbook.core.eventbus import EventBus
from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    Candle,
    CandleBar,
    CandleHistory,
    Event,
    InstrumentInfo,
    PriceLevel,
    Side,
    Ticker24h,
    Trade,
)
from cockbook.core.instrument import Instrument

log = logging.getLogger(__name__)

EXCHANGE = "bitunix"
_ZERO = Decimal(0)
# Max. Abos je Verbindung laut Doku
_MAX_SUBS = 300
# Kline-REST laut Doku: max. 200 Kerzen je Abfrage, 10 Abfragen/s
_KLINE_PAGE = 200
_KLINE_PAUSE_S = 0.2
_MINUTE_NS = 60_000_000_000


def _decimal_step(precision: Any) -> Decimal:
    """Nachkommastellen -> Schrittweite (1 -> 0.1, 4 -> 0.0001, 0 -> 1)."""
    return Decimal(1).scaleb(-int(precision))


def parse_pairs(data: list[dict[str, Any]]) -> dict[str, Instrument]:
    """Handelbare Paare (``symbolStatus == OPEN``) mit Tick-/Step-Größe.

    Bitunix nennt keine Tick-Größe, sondern ``quotePrecision`` (Nachkommastellen des Preises);
    die kleinste Preisstufe ist daher 10^-quotePrecision."""
    out: dict[str, Instrument] = {}
    for p in data:
        if p.get("symbolStatus") != "OPEN":
            continue
        sym = str(p["symbol"])
        out[sym] = Instrument(
            exchange=EXCHANGE,
            symbol=sym,
            tick_size=_decimal_step(p["quotePrecision"]),
            step_size=_decimal_step(p["basePrecision"]),
        )
    return out


def parse_trade(item: dict[str, Any], inst: Instrument, ts_ms: int, recv_ns: int) -> Trade:
    # "s" = Seite des Takers: buy = aggressiver Käufer
    return Trade(
        exchange=EXCHANGE,
        symbol=inst.symbol,
        ts_exchange_ns=ts_ms * 1_000_000,
        ts_recv_ns=recv_ns,
        price_ticks=inst.price_to_ticks_rounded(item["p"]),
        qty=Decimal(item["v"]),
        aggressor=Side.BID if item["s"] == "buy" else Side.ASK,
    )


def parse_klines(data: list[dict[str, Any]]) -> list[CandleBar]:
    """Kline-REST -> Kerzen, aufsteigend sortiert (Bitunix liefert neueste zuerst)."""
    bars = [
        CandleBar(
            open_time_ns=int(d["time"]) * 1_000_000,
            open=Decimal(d["open"]),
            high=Decimal(d["high"]),
            low=Decimal(d["low"]),
            close=Decimal(d["close"]),
            volume=Decimal(d["baseVol"]),
        )
        for d in data
    ]
    return sorted(bars, key=lambda b: b.open_time_ns)


async def fetch_klines(
    session: aiohttp.ClientSession,
    rest_url: str,
    symbol: str,
    interval: str,
    limit: int,
    stop: asyncio.Event | None = None,
) -> list[CandleBar]:
    """Die letzten ``limit`` abgeschlossenen Kerzen, rückwärts in Seiten zu 200
    (Doku: max. 200 je Abfrage, 10 Abfragen/s). Aufsteigend sortiert."""
    bars: dict[int, CandleBar] = {}
    end_ms: int | None = None
    while len(bars) < limit and not (stop is not None and stop.is_set()):
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(_KLINE_PAGE, limit - len(bars)),
        }
        if end_ms is not None:
            params["endTime"] = end_ms
        data, _ = await get_json(session, f"{rest_url}/api/v1/futures/market/kline", params)
        page = parse_klines(data.get("data") or [])
        new = [b for b in page if b.open_time_ns not in bars]
        if not new:
            break
        bars.update((b.open_time_ns, b) for b in new)
        end_ms = page[0].open_time_ns // 1_000_000 - 1
        if len(bars) < limit:
            await asyncio.sleep(_KLINE_PAUSE_S)
    return [bars[t] for t in sorted(bars)]


class BitunixKlines:
    """Kerzen auf Anfrage für die Langzeit-Ansicht (5m/15m/1h/4h), kurz zwischengespeichert
    wie ``BinanceKlines`` – die Ansicht fragt jede Minute nach."""

    def __init__(self, cfg: BitunixConfig, ttl_s: float = 30.0) -> None:
        self.cfg = cfg
        self.ttl_s = ttl_s
        self._cache: dict[tuple[str, str, int], tuple[float, list[CandleBar]]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def get(self, symbol: str, interval: str, limit: int) -> list[CandleBar]:
        key = (symbol.upper(), interval, limit)
        hit = self._cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        if self._session is None or self._session.closed:
            self._session = _session()
        try:
            bars = await fetch_klines(self._session, self.cfg.rest_url, key[0], interval, limit)
        except (RestError, aiohttp.ClientError, TimeoutError, KeyError, ValueError) as e:
            log.warning("bitunix klines failed", extra={"symbol": symbol, "error": repr(e)})
            return hit[1] if hit else []
        self._cache[key] = (time.monotonic(), bars)
        return bars

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class MinuteCandleBuilder:
    """Laufende 1m-Kerze aus Trades (der REST liefert nur abgeschlossene Kerzen).

    Die Minute, in der das Orderbuch startet, enthält nur die Trades ab dem Start.
    """

    def __init__(self, inst: Instrument) -> None:
        self.inst = inst
        self.bar: CandleBar | None = None
        # zuletzt abgeschlossene Minute: spätere Trades dafür ändern nichts mehr
        self.closed_minute: int | None = None

    def roll(self, now_ns: int) -> list[Event]:
        """Minute vorbei, aber kein neuer Trade: laufende Kerze trotzdem abschließen (sonst
        bliebe sie offen, bis irgendwann der nächste Trade kommt)."""
        b = self.bar
        if b is None or b.open_time_ns >= now_ns // _MINUTE_NS * _MINUTE_NS:
            return []
        self.bar = None
        self.closed_minute = b.open_time_ns
        return [self._event(b, closed=True, ts=now_ns)]

    def on_trade(self, t: Trade) -> list[Event]:
        price = t.price_ticks * self.inst.tick_size
        minute = t.ts_ns // _MINUTE_NS * _MINUTE_NS
        out: list[Event] = []
        b = self.bar
        if (b is not None and minute < b.open_time_ns) or (
            self.closed_minute is not None and minute <= self.closed_minute
        ):
            return out  # verspäteter Trade einer abgeschlossenen Minute
        if b is not None and minute > b.open_time_ns:
            out.append(self._event(b, closed=True, ts=t.ts_recv_ns))
            self.closed_minute = b.open_time_ns
            b = None
        if b is None:
            b = CandleBar(minute, price, price, price, price, t.qty)
        else:
            b = CandleBar(b.open_time_ns, b.open, max(b.high, price), min(b.low, price), price,
                          b.volume + t.qty)  # fmt: skip
        self.bar = b
        out.append(self._event(b, closed=False, ts=t.ts_recv_ns))
        return out

    def _event(self, bar: CandleBar, *, closed: bool, ts: int) -> Candle:
        return Candle(exchange=EXCHANGE, symbol=self.inst.symbol, ts_recv_ns=ts, interval="1m",
                      bar=bar, closed=closed)  # fmt: skip


def parse_ticker(d: dict[str, Any], ts_ms: int, recv_ns: int) -> Ticker24h:
    return Ticker24h(
        exchange=EXCHANGE,
        symbol=str(d["s"]),
        ts_exchange_ns=ts_ms * 1_000_000,
        ts_recv_ns=recv_ns,
        last=Decimal(d["la"]),
        open=Decimal(d["o"]),
        high=Decimal(d["h"]),
        low=Decimal(d["l"]),
        volume=Decimal(d["b"]),
        quote_volume=Decimal(d["q"]),
    )


class BitunixBookDiffer:
    """Volles Buch je Nachricht -> Snapshot (erste) bzw. Delta (nur Änderungen)."""

    def __init__(self, inst: Instrument) -> None:
        self.inst = inst
        self._bids: dict[str, str] | None = None
        self._asks: dict[str, str] = {}
        self._seq = 0

    @property
    def synced(self) -> bool:
        return self._bids is not None

    def reset(self) -> None:
        self._bids = None
        self._asks = {}

    def _levels(self, items: dict[str, str]) -> tuple[PriceLevel, ...]:
        return tuple(PriceLevel(self.inst.price_to_ticks(p), Decimal(q)) for p, q in items.items())

    def on_book(self, data: dict[str, Any], ts_ms: int, recv_ns: int) -> list[Event]:
        bids = {p: q for p, q in data.get("b", [])}
        asks = {p: q for p, q in data.get("a", [])}
        common: dict[str, Any] = {
            "exchange": EXCHANGE,
            "symbol": self.inst.symbol,
            "ts_exchange_ns": ts_ms * 1_000_000,
            "ts_recv_ns": recv_ns,
        }
        self._seq += 1
        if self._bids is None:
            self._bids, self._asks = bids, asks
            return [
                BookSnapshot(
                    **common,
                    bids=self._levels(bids),
                    asks=self._levels(asks),
                    last_seq=self._seq,
                ),
                BookSyncStatus(**common, synced=True, reason="snapshot"),
            ]
        changes: list[tuple[PriceLevel, ...]] = []
        for old, new in ((self._bids, bids), (self._asks, asks)):
            diff = {p: q for p, q in new.items() if old.get(p) != q}
            levels = list(self._levels(diff))
            levels += [PriceLevel(self.inst.price_to_ticks(p), _ZERO) for p in old if p not in new]
            changes.append(tuple(levels))
        self._bids, self._asks = bids, asks
        if not changes[0] and not changes[1]:
            return []
        return [
            BookDelta(
                **common,
                bids=changes[0],
                asks=changes[1],
                first_seq=self._seq,
                last_seq=self._seq,
                prev_last_seq=self._seq - 1,
            )
        ]


def _session() -> aiohttp.ClientSession:
    # trust_env: Proxy-Einstellungen aus der Umgebung respektieren (Firmen-/Cloud-Netze)
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True)


async def ping_loop(get_ws: Any, stop: asyncio.Event, interval_s: float) -> None:
    """JSON-Ping laut Doku; ein Ping je Intervall bleibt weit unter 5 Nachrichten/s."""
    while not await sleep_or_stop(stop, interval_s):
        ws = get_ws()
        if ws is not None and not ws.closed:
            # Bricht die Verbindung gerade ab, verbindet run_ws ohnehin neu
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.send_str(json.dumps({"op": "ping", "ping": int(time.time())}))


class BitunixAdapter:
    """Orderbuch + Trades eines Symbols."""

    def __init__(self, cfg: BitunixConfig, symbol: str, bus: EventBus) -> None:
        self.cfg = cfg
        self.symbol = symbol.upper()
        self.name = f"{EXCHANGE}:{self.symbol}"
        self.bus = bus
        self.inst: Instrument | None = None
        self.differ: BitunixBookDiffer | None = None
        self.candles: MinuteCandleBuilder | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _publish(self, events: list[Event]) -> None:
        for ev in events:
            await self.bus.publish(ev)

    async def run(self) -> None:
        async with _session() as session:
            data, _ = await get_json(
                session,
                f"{self.cfg.rest_url}/api/v1/futures/market/trading_pairs",
                {"symbols": self.symbol},
            )
            insts = parse_pairs(data.get("data") or [])
            if self.symbol not in insts:
                raise RuntimeError(f"{self.symbol} ist auf Bitunix nicht handelbar")
            self.inst = inst = insts[self.symbol]
            self.differ = BitunixBookDiffer(inst)
            self.candles = MinuteCandleBuilder(inst)
            now = time.time_ns()
            await self._publish(
                [
                    InstrumentInfo(
                        exchange=EXCHANGE,
                        symbol=self.symbol,
                        ts_recv_ns=now,
                        tick_size=inst.tick_size,
                        step_size=inst.step_size,
                    )
                ]
            )
            log.info("bitunix adapter starting", extra={"symbol": self.symbol})
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._candle_history(session))
                if self.cfg.candle_reconcile_bars > 0:
                    tg.create_task(self._candle_reconcile(session))
                tg.create_task(ping_loop(lambda: self._ws, self._stop, self.cfg.ping_interval_s))
                tg.create_task(
                    run_ws(
                        session,
                        self.cfg.ws_url,
                        name=self.name,
                        cfg=self.cfg.reconnect,
                        stop=self._stop,
                        on_message=self._on_message,
                        on_ws=self._on_ws,
                        on_close=self._on_close,
                    )
                )
        log.info("bitunix adapter stopped", extra={"symbol": self.symbol})

    async def _candle_history(self, session: aiohttp.ClientSession) -> None:
        """1m-Kerzen beim Start. Fehler sind nicht fatal: Ohne Historie zeigt der Chart eben
        nur die Kerzen ab jetzt."""
        try:
            bars = await fetch_klines(
                session, self.cfg.rest_url, self.symbol, "1m", self.cfg.candle_history_bars,
                self._stop,
            )  # fmt: skip
        except (RestError, aiohttp.ClientError, TimeoutError, KeyError, ValueError) as e:
            log.warning("bitunix klines failed", extra={"symbol": self.symbol, "error": repr(e)})
            return
        if bars and not self.bus.closed:
            await self.bus.publish(
                CandleHistory(
                    exchange=EXCHANGE,
                    symbol=self.symbol,
                    ts_recv_ns=time.time_ns(),
                    interval="1m",
                    source="futures",
                    bars=tuple(bars),
                )
            )

    async def _candle_reconcile(self, session: aiohttp.ClientSession) -> None:
        """Jede Minute kurz nach dem Wechsel: laufende Kerze abschließen und die letzten
        abgeschlossenen Kerzen durch die offiziellen der Börse ersetzen.

        Warum nicht der Kline-Kanal der Bitunix-WebSocket? Dessen Pushes tragen keine
        Öffnungszeit, nur den Sendezeitpunkt – an der Minutengrenze ist nicht eindeutig, zu
        welcher Kerze ein Wert gehört. Der REST-Abgleich dagegen ist exakt und füllt auch
        Minuten ohne Trade sowie Lücken nach einer Trennung.
        Doku: https://www.bitunix.com/api-docs/futures/market/get_kline.html
        """
        last_ok: int | None = None
        while not await sleep_to_minute(self._stop, self.cfg.candle_reconcile_delay_s):
            now = time.time_ns()
            if self.candles is not None and not self.bus.closed:
                await self._publish(self.candles.roll(now))
            n = bars_to_fetch(
                last_ok, now, self.cfg.candle_reconcile_bars, max(self.cfg.candle_history_bars, 1)
            )
            try:
                bars = await fetch_klines(
                    session, self.cfg.rest_url, self.symbol, "1m", n + 1, self._stop
                )
            except (RestError, aiohttp.ClientError, TimeoutError, KeyError, ValueError) as e:
                log.warning(
                    "bitunix candle reconcile failed",
                    extra={"symbol": self.symbol, "error": repr(e)},
                )
                continue
            now = time.time_ns()
            closed = tuple(b for b in bars if b.open_time_ns + _MINUTE_NS <= now)
            last_ok = now
            if closed and not self.bus.closed:
                await self.bus.publish(
                    CandleHistory(
                        exchange=EXCHANGE,
                        symbol=self.symbol,
                        ts_recv_ns=now,
                        interval="1m",
                        source="futures",
                        bars=closed,
                    )
                )

    async def _on_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        args = [
            {"symbol": self.symbol, "ch": "depth_books"},
            {"symbol": self.symbol, "ch": "trade"},
        ]
        await ws.send_str(json.dumps({"op": "subscribe", "args": args}))

    async def _on_close(self, reason: str) -> None:
        self._ws = None
        if self.differ is None or self.bus.closed:
            return
        was_synced = self.differ.synced
        self.differ.reset()
        if was_synced:
            # Ab hier unbekannt, bis der nächste volle Snapshot kommt
            await self.bus.publish(
                BookSyncStatus(
                    exchange=EXCHANGE,
                    symbol=self.symbol,
                    ts_recv_ns=time.time_ns(),
                    synced=False,
                    reason=f"disconnect: {reason}",
                )
            )

    async def _on_message(self, text: str, recv_ns: int) -> None:
        msg = json.loads(text)
        ch = msg.get("ch")
        if ch is None or msg.get("symbol") != self.symbol:
            return  # connect/ping/subscribe-Bestätigungen
        assert self.differ is not None
        assert self.inst is not None
        ts_ms = int(msg.get("ts") or recv_ns // 1_000_000)
        if ch == "depth_books":
            await self._publish(self.differ.on_book(msg["data"], ts_ms, recv_ns))
        elif ch == "trade":
            assert self.candles is not None
            trades = [parse_trade(t, self.inst, ts_ms, recv_ns) for t in msg["data"]]
            await self._publish([*trades, *(e for t in trades for e in self.candles.on_trade(t))])


class BitunixMarketStats:
    """Handelspaare (``InstrumentInfo``) und 24h-Statistik für alle Bitunix-Coins.

    ``tickers`` braucht ein Abo je Symbol; bei max. 300 Abos je Verbindung werden die
    Symbole auf mehrere Verbindungen verteilt (ein Abo-Kommando je Verbindung)."""

    name = f"{EXCHANGE}:stats"

    def __init__(
        self,
        cfg: BitunixConfig,
        wanted: list[str] | None,
        bus: EventBus,
        categories: dict[str, str] | None = None,
    ) -> None:
        """``wanted=None``: alle handelbaren Paare; sonst nur diese."""
        self.cfg = cfg
        self.wanted = [w.upper() for w in wanted] if wanted is not None else None
        self.categories = categories or {}
        self.bus = bus
        self.available: dict[str, Instrument] = {}
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        async with _session() as session:
            data, _ = await get_json(
                session, f"{self.cfg.rest_url}/api/v1/futures/market/trading_pairs"
            )
            pairs = parse_pairs(data.get("data") or [])
            syms = sorted(pairs) if self.wanted is None else [s for s in self.wanted if s in pairs]
            now = time.time_ns()
            for sym in syms:
                inst = pairs[sym]
                self.available[sym] = inst
                cat = self.categories.get(sym, "crypto")
                await self.bus.publish(
                    InstrumentInfo(
                        exchange=EXCHANGE,
                        symbol=sym,
                        ts_recv_ns=now,
                        tick_size=inst.tick_size,
                        step_size=inst.step_size,
                        kind="crypto" if cat == "crypto" else "tradfi",
                        category=cat,
                    )
                )
            self.ready.set()
            log.info("bitunix stats starting", extra={"symbols": len(syms)})
            chunks = [syms[i : i + _MAX_SUBS] for i in range(0, len(syms), _MAX_SUBS)]
            async with asyncio.TaskGroup() as tg:
                for n, chunk in enumerate(chunks):
                    tg.create_task(self._run_chunk(session, n, chunk))

    async def _run_chunk(self, session: aiohttp.ClientSession, n: int, syms: list[str]) -> None:
        holder: dict[str, aiohttp.ClientWebSocketResponse | None] = {"ws": None}

        async def on_ws(ws: aiohttp.ClientWebSocketResponse) -> None:
            holder["ws"] = ws
            args = [{"symbol": s, "ch": "tickers"} for s in syms]
            await ws.send_str(json.dumps({"op": "subscribe", "args": args}))

        async with asyncio.TaskGroup() as tg:
            tg.create_task(ping_loop(lambda: holder["ws"], self._stop, self.cfg.ping_interval_s))
            tg.create_task(
                run_ws(
                    session,
                    self.cfg.ws_url,
                    name=f"{self.name}:{n}",
                    cfg=self.cfg.reconnect,
                    stop=self._stop,
                    on_message=self._on_message,
                    on_ws=on_ws,
                )
            )

    async def _on_message(self, text: str, recv_ns: int) -> None:
        msg = json.loads(text)
        if msg.get("ch") != "tickers":
            return
        ts_ms = int(msg.get("ts") or recv_ns // 1_000_000)
        for d in msg.get("data") or []:
            if d.get("s") in self.available:
                await self.bus.publish(parse_ticker(d, ts_ms, recv_ns))
