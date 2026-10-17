"""Binance USDⓈ-M Futures Adapter.

Referenzen (gelesen am 2026-10-13, Seiten wurden an diesem Tag zuletzt geändert):

- Lokales Orderbuch:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly
- Verbindung, Routing, Limits:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Connect
- Stream-Referenz (public: Depth / market: aggTrade, forceOrder):
  https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public
  https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market
- REST (Order Book, Exchange Information):
  https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data

Wichtige Fakten aus der Doku
----------------------------
- Streams sind auf geroutete Endpoints verteilt: ``/public`` (Depth, BookTicker) und
  ``/market`` (aggTrade, forceOrder, …). Ein ``/market``-Stream auf einer
  ``/public``-Verbindung liefert *still nichts* – daher zwei Verbindungen.
- Verbindungen werden nach 24 h getrennt; Server-Ping alle 3 min, ohne Pong Trennung
  nach 10 min. Max. 10 eingehende Nachrichten/s pro Verbindung (wir senden keine:
  Abo per URL).
- Depth-Snapshot ``GET /fapi/v1/depth`` limit=1000 kostet Gewicht 20 (Limit 2400/min).
- RPI-Orders sind weder im Diff-Stream noch im REST-Snapshot enthalten – konsistent.

Sync-Prozedur (wörtlich nach Doku)
----------------------------------
1. Stream ``<symbol>@depth`` öffnen, Events puffern.
2. Snapshot von ``/fapi/v1/depth?symbol=…&limit=1000`` holen.
3. Events mit ``u < lastUpdateId`` verwerfen.
4. Das erste verarbeitete Event muss ``U <= lastUpdateId`` UND ``u >= lastUpdateId``
   erfüllen.
5. Danach muss für jedes Event ``pu == u`` des vorherigen gelten, sonst ab Schritt 2
   neu initialisieren.
6. Mengen sind absolut; 0 = Level entfernen; Entfernen eines unbekannten Levels ist
   normal.

Trades: ``aggTrade`` statt ``trade``
-----------------------------------
Für USDⓈ-M Futures gibt es laut aktueller Stream-Referenz *keinen* öffentlichen
Raw-``@trade``-Stream – nur ``@aggTrade``. Das ist für uns auch inhaltlich passend:
ein aggTrade fasst Fills *eines Takers* zum selben Preis zusammen (alle Fills einer
Market-Order auf einem Level). Genau diese Einheit wollen Trade-Bubbles zeigen.
Aggressor: ``m == true`` (Käufer ist Maker) => Verkäufer war Taker => ``Side.ASK``.

Marktkennzahlen (gelesen am 2026-10-16)
---------------------------------------
- ``<symbol>@markPrice@1s`` (/market): Mark-/Indexpreis, Funding-Rate ``r``, nächste
  Funding-Zeit ``T``.
- ``<symbol>@miniTicker`` (/market): rollierende 24h-Statistik (o/h/l/c, Volumen).
- REST ``/fapi/v1/openInterest`` (Gewicht 1): aktuelles Open Interest in Kontrakten.
- REST ``/futures/data/globalLongShortAccountRatio`` und ``/futures/data/takerlongshortRatio``
  (Gewicht 0, Limit 1000 Requests/5 min): Werte pro Periode (5m … 1d), nur 30 Tage.
- *Nicht* genutzt: ``topLongShortAccountRatio``/``topLongShortPositionRatio`` – laut
  aktueller Doku nur noch mit API-Key (``X-MBX-APIKEY``); CockBook kommt ohne Account aus.

Liquidationen: gedrosselt!
--------------------------
``<symbol>@forceOrder`` liefert laut Doku pro Symbol nur die *letzte*
Liquidationsorder innerhalb von 1000 ms als Snapshot. Mehrere Liquidationen in einer
Sekunde werden also nicht einzeln geliefert – die Summe ist eine Untergrenze. Das
ist eine Börsen-Einschränkung, keine des Adapters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import aiohttp

from cockbook.adapters.common import RestError, get_json, run_ws, sleep_or_stop
from cockbook.config import BinanceUsdmConfig
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
    Liquidation,
    LongShortRatio,
    MarketSession,
    MarkPrice,
    OpenInterest,
    PriceLevel,
    RawMessage,
    Side,
    TakerVolume,
    Ticker24h,
    Trade,
)
from cockbook.core.instrument import Instrument
from cockbook.core.orderbook import OrderBook

log = logging.getLogger(__name__)

EXCHANGE = "binance_usdm"
_MS = 1_000_000  # ms -> ns


# ======================================================================================
# Parsing (reine Funktionen)
# ======================================================================================


@dataclass(frozen=True, slots=True)
class DepthUpdate:
    first_id: int  # U
    final_id: int  # u
    prev_final_id: int  # pu
    transact_time_ms: int  # T
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    recv_ns: int


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    last_update_id: int
    transact_time_ms: int | None
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    recv_ns: int


def _levels(inst: Instrument, raw: list[list[str]]) -> tuple[PriceLevel, ...]:
    return tuple(PriceLevel(inst.price_to_ticks(p), Decimal(q)) for p, q in raw)


def parse_depth_update(d: dict[str, Any], inst: Instrument, recv_ns: int) -> DepthUpdate:
    return DepthUpdate(
        first_id=int(d["U"]),
        final_id=int(d["u"]),
        prev_final_id=int(d["pu"]),
        transact_time_ms=int(d["T"]),
        bids=_levels(inst, d["b"]),
        asks=_levels(inst, d["a"]),
        recv_ns=recv_ns,
    )


def parse_depth_snapshot(d: dict[str, Any], inst: Instrument, recv_ns: int) -> DepthSnapshot:
    t = d.get("T")
    return DepthSnapshot(
        last_update_id=int(d["lastUpdateId"]),
        transact_time_ms=int(t) if t is not None else None,
        bids=_levels(inst, d["bids"]),
        asks=_levels(inst, d["asks"]),
        recv_ns=recv_ns,
    )


def parse_agg_trade(d: dict[str, Any], inst: Instrument, recv_ns: int) -> Trade:
    return Trade(
        exchange=EXCHANGE,
        symbol=inst.symbol,
        ts_exchange_ns=int(d["T"]) * _MS,
        ts_recv_ns=recv_ns,
        price_ticks=inst.price_to_ticks(d["p"]),
        qty=Decimal(d["q"]),
        # m = "buyer is maker" -> dann war der Verkäufer aggressiv
        aggressor=Side.ASK if d["m"] else Side.BID,
        trade_id=str(d["a"]),
    )


def parse_force_order(d: dict[str, Any], inst: Instrument, recv_ns: int) -> Liquidation:
    o = d["o"]
    # ap = Durchschnittspreis der Ausführung; kann zwischen Ticks liegen -> runden.
    # Fallback auf p (Orderpreis), falls ap fehlt oder 0 ist.
    price = o.get("ap") or "0"
    if Decimal(price) == 0:
        price = o["p"]
    filled = Decimal(o.get("z") or "0")
    return Liquidation(
        exchange=EXCHANGE,
        symbol=inst.symbol,
        ts_exchange_ns=int(o["T"]) * _MS,
        ts_recv_ns=recv_ns,
        price_ticks=inst.price_to_ticks_rounded(price),
        price=Decimal(price),
        qty=filled if filled > 0 else Decimal(o["q"]),
        # Seite der Liquidations-Order: SELL = Long wird zwangsverkauft
        side=Side.ASK if o["S"] == "SELL" else Side.BID,
    )


def _ms(v: Any) -> int | None:
    return int(v) * _MS if v else None


def parse_mark_price(d: dict[str, Any], symbol: str, recv_ns: int) -> MarkPrice:
    return MarkPrice(
        exchange=EXCHANGE,
        symbol=symbol,
        ts_exchange_ns=_ms(d.get("E")),
        ts_recv_ns=recv_ns,
        mark_price=Decimal(d["p"]),
        index_price=Decimal(d["i"]) if d.get("i") else None,
        funding_rate=Decimal(d.get("r") or "0"),
        next_funding_ns=_ms(d.get("T")),
    )


def parse_mini_ticker(d: dict[str, Any], symbol: str, recv_ns: int) -> Ticker24h:
    return Ticker24h(
        exchange=EXCHANGE,
        symbol=symbol,
        ts_exchange_ns=_ms(d.get("E")),
        ts_recv_ns=recv_ns,
        last=Decimal(d["c"]),
        open=Decimal(d["o"]),
        high=Decimal(d["h"]),
        low=Decimal(d["l"]),
        volume=Decimal(d["v"]),
        quote_volume=Decimal(d["q"]),
    )


def parse_open_interest(d: dict[str, Any], recv_ns: int) -> OpenInterest:
    return OpenInterest(
        exchange=EXCHANGE,
        symbol=d["symbol"],
        ts_exchange_ns=_ms(d.get("time")),
        ts_recv_ns=recv_ns,
        open_interest=Decimal(d["openInterest"]),
    )


def parse_long_short(
    rows: list[dict[str, Any]], symbol: str, period: str, recv_ns: int
) -> LongShortRatio | None:
    if not rows:
        return None
    d = rows[-1]  # aufsteigend sortiert -> letzter = aktuellster
    return LongShortRatio(
        exchange=EXCHANGE,
        symbol=symbol,
        ts_exchange_ns=_ms(d.get("timestamp")),
        ts_recv_ns=recv_ns,
        period=period,
        long_share=Decimal(d["longAccount"]),
        short_share=Decimal(d["shortAccount"]),
        ratio=Decimal(d["longShortRatio"]),
    )


def parse_taker_volume(
    rows: list[dict[str, Any]], symbol: str, period: str, recv_ns: int
) -> TakerVolume | None:
    if not rows:
        return None
    d = rows[-1]
    return TakerVolume(
        exchange=EXCHANGE,
        symbol=symbol,
        ts_exchange_ns=_ms(d.get("timestamp")),
        ts_recv_ns=recv_ns,
        period=period,
        buy_volume=Decimal(d["buyVol"]),
        sell_volume=Decimal(d["sellVol"]),
        ratio=Decimal(d["buySellRatio"]),
    )


def parse_kline_row(row: list[Any]) -> CandleBar:
    """REST-Kline: [open time, open, high, low, close, volume, close time, …]."""
    return CandleBar(
        open_time_ns=int(row[0]) * _MS,
        open=Decimal(row[1]),
        high=Decimal(row[2]),
        low=Decimal(row[3]),
        close=Decimal(row[4]),
        volume=Decimal(row[5]),
    )


def parse_kline_ws(d: dict[str, Any], symbol: str, recv_ns: int) -> Candle:
    k = d["k"]
    return Candle(
        exchange=EXCHANGE,
        symbol=symbol,
        ts_exchange_ns=_ms(d.get("E")),
        ts_recv_ns=recv_ns,
        interval=k["i"],
        bar=CandleBar(
            open_time_ns=int(k["t"]) * _MS,
            open=Decimal(k["o"]),
            high=Decimal(k["h"]),
            low=Decimal(k["l"]),
            close=Decimal(k["c"]),
            volume=Decimal(k["v"]),
        ),
        closed=bool(k["x"]),
    )


# underlyingType von Krypto-Kontrakten (COIN = Coin, INDEX = Krypto-Index wie BTCDOM,
# PREMARKET = Krypto vor dem Spot-Listing). Alles andere ist ein TradFi-Underlying.
_CRYPTO_UNDERLYINGS = {"", "COIN", "INDEX", "PREMARKET"}


def is_tradfi(symbol_entry: dict[str, Any], overrides: dict[str, str] | None = None) -> bool:
    """TradFi-Kontrakt (Aktie, ETF, Rohstoff, Devise)?

    Binance markiert nicht alle so: ``contractType = TRADIFI_PERPETUAL`` tragen laut einer
    Stichprobe vom 15.09.2026 nur 22 Symbole (Gold, Öl, einige Aktien), obwohl z. B. QQQ,
    SPY, AAPL seit April 2026 handelbar sind. Deshalb zusätzlich: ein ``underlyingType``,
    der kein Krypto ist (EQUITY, COMMODITY, FX …), oder das Symbol steht in der Config.
    """
    if str(symbol_entry.get("contractType", "")).upper() == "TRADIFI_PERPETUAL":
        return True
    if overrides and str(symbol_entry.get("symbol", "")) in overrides:
        return True
    return str(symbol_entry.get("underlyingType", "")).upper() not in _CRYPTO_UNDERLYINGS


# underlyingType der TradFi-Kontrakte -> Markt im tradingSession-Stream
# (EquityUpdate -> EQUITY, KR_EquityUpdate -> KR_EQUITY, …). PREMARKET = US-Aktien vor
# dem Börsengang/Pre-Market-Kontrakte, gehören zu den US-Handelszeiten.
_TRADFI_MARKETS = {
    "EQUITY": "EQUITY",
    "PREMARKET": "EQUITY",
    "KR_EQUITY": "KR_EQUITY",
    "HK_EQUITY": "HK_EQUITY",
    "CN_EQUITY": "CN_EQUITY",
    "COMMODITY": "COMMODITY",
    "FX": "FX",
}
CATEGORIES = ("crypto", "stock", "etf", "commodity", "fx", "tradfi")


def classify(
    symbol_entry: dict[str, Any], overrides: dict[str, str] | None = None
) -> tuple[str, str, str | None]:
    """(kind, category, market) eines Symbols aus der exchangeInfo.

    ``underlyingType`` unterscheidet Aktien (``*EQUITY``), Rohstoffe (``COMMODITY``) und
    Devisen (``FX``). ETFs laufen bei Binance ebenfalls als Aktien-Underlying; erkannt
    werden sie über ``underlyingSubType`` (falls dort "ETF" steht) oder die Config.
    """
    sym = str(symbol_entry.get("symbol", ""))
    if not is_tradfi(symbol_entry, overrides):
        return "crypto", "crypto", None
    ut = str(symbol_entry.get("underlyingType", "")).upper()
    subs = {str(x).upper() for x in symbol_entry.get("underlyingSubType") or []}
    market = _TRADFI_MARKETS.get(ut)
    if overrides and sym in overrides and market is None:
        # Underlying unbekannt -> Handelszeiten aus der Kategorie ableiten
        market = {"stock": "EQUITY", "etf": "EQUITY", "commodity": "COMMODITY", "fx": "FX"}.get(
            overrides[sym]
        )
    if overrides and sym in overrides:
        category = overrides[sym]
    elif "ETF" in subs:
        category = "etf"
    elif ut.endswith("EQUITY") or ut == "PREMARKET":
        category = "stock"
    elif ut == "COMMODITY":
        category = "commodity"
    elif ut == "FX":
        category = "fx"
    else:
        category = "tradfi"
    return "tradfi", category, market


def instrument_info(
    entry: dict[str, Any], inst: Instrument, recv_ns: int, overrides: dict[str, str] | None
) -> InstrumentInfo:
    kind, category, market = classify(entry, overrides)
    return InstrumentInfo(
        exchange=EXCHANGE,
        symbol=inst.symbol,
        ts_recv_ns=recv_ns,
        tick_size=inst.tick_size,
        step_size=inst.step_size,
        kind=kind,
        category=category,
        market=market,
    )


def parse_instruments(exchange_info: dict[str, Any]) -> dict[str, Instrument]:
    """Alle Symbole der exchangeInfo (für den Liquidations-Stream des Gesamtmarkts)."""
    out: dict[str, Instrument] = {}
    for s in exchange_info["symbols"]:
        filters = {f["filterType"]: f for f in s.get("filters", [])}
        if "PRICE_FILTER" in filters and "LOT_SIZE" in filters:
            out[s["symbol"]] = Instrument(
                exchange=EXCHANGE,
                symbol=s["symbol"],
                tick_size=Decimal(filters["PRICE_FILTER"]["tickSize"]),
                step_size=Decimal(filters["LOT_SIZE"]["stepSize"]),
            )
    return out


def parse_instrument(exchange_info: dict[str, Any], symbol: str) -> Instrument:
    for s in exchange_info["symbols"]:
        if s["symbol"] == symbol:
            filters = {f["filterType"]: f for f in s["filters"]}
            return Instrument(
                exchange=EXCHANGE,
                symbol=symbol,
                # Doku: pricePrecision/quantityPrecision NICHT als Tick/Step verwenden.
                tick_size=Decimal(filters["PRICE_FILTER"]["tickSize"]),
                step_size=Decimal(filters["LOT_SIZE"]["stepSize"]),
            )
    raise ValueError(f"Symbol {symbol} nicht in exchangeInfo gefunden")


def unwrap_combined(text: str) -> tuple[str, dict[str, Any]]:
    """Combined-Stream-Nachrichten haben die Form ``{"stream": …, "data": …}``."""
    msg = json.loads(text)
    return msg["stream"], msg["data"]


def depth_stream_name(symbol: str, update_ms: int) -> str:
    base = f"{symbol.lower()}@depth"
    return base if update_ms == 250 else f"{base}@{update_ms}ms"


# ======================================================================================
# Sync-Zustandsautomat (kein I/O -> deterministisch testbar, später im Replay wiederverwendet)
# ======================================================================================


class StaleSnapshotError(Exception):
    """Snapshot ist älter als das älteste gepufferte Event -> neuen Snapshot holen."""


@dataclass(frozen=True, slots=True)
class _Applied:
    prev_id: int  # Update-ID, auf der dieses Event aufsetzt
    final_id: int
    bid_prices: frozenset[int]
    ask_prices: frozenset[int]


@dataclass(slots=True)
class SyncStats:
    syncs: int = 0
    gaps: int = 0
    stale_snapshots: int = 0
    buffer_overflows: int = 0
    crossed: int = 0
    coverage_refreshes: int = 0
    validations_ok: int = 0
    validations_failed: int = 0
    validations_skipped: int = 0


@dataclass(slots=True)
class ValidationResult:
    snapshot_id: int
    local_id: int
    compared: int
    excluded_touched: int
    mismatches: list[tuple[str, int, Decimal, Decimal]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


class BinanceDepthSync:
    """Hält ein lokales Buch synchron zum Diff-Stream, exakt nach Binance-Prozedur.

    Eingaben: ``on_depth`` (Stream-Event), ``on_snapshot`` (REST-Snapshot),
    ``request_validation`` (Kontroll-Snapshot). Ausgaben: normalisierte Events in
    garantiert sauberer Reihenfolge – ``BookSnapshot``, dann nur lückenlose
    ``BookDelta``. Bei jedem Problem: ``BookSyncStatus(synced=False)`` und Neu-Sync.
    """

    def __init__(
        self,
        inst: Instrument,
        *,
        max_buffer: int = 10_000,
        history: int = 600,
        resync_on_mismatch: bool = True,
        coverage_refresh_fraction: float = 0.5,
    ) -> None:
        self.inst = inst
        self.book = OrderBook()
        self.stats = SyncStats()
        self.last_validation: ValidationResult | None = None
        self._buffer: deque[DepthUpdate] = deque(maxlen=max_buffer)
        self._history: deque[_Applied] = deque(maxlen=history)
        self._resync_on_mismatch = resync_on_mismatch
        self._coverage_fraction = coverage_refresh_fraction
        self._synced = False
        self._awaiting_first = False
        self._snapshot_id = 0
        self._last_id = 0
        self._mid_bounds: tuple[float, float] | None = None
        self._coverage: tuple[int, int] | None = None
        self._pending_validation: DepthSnapshot | None = None
        self._refresh_wanted = False
        self._pending_refresh: DepthSnapshot | None = None

    # --- Zustand ------------------------------------------------------------------------

    @property
    def synced(self) -> bool:
        return self._synced

    @property
    def wants_snapshot(self) -> bool:
        """Doku Schritt 1->2: erst puffern, dann Snapshot holen."""
        return not self._synced and bool(self._buffer)

    @property
    def wants_refresh(self) -> bool:
        """Mid nähert sich dem Rand des Snapshot-Bereichs -> frischen Snapshot holen,
        ohne das Buch zu verwerfen (siehe ``on_refresh_snapshot``)."""
        return self._synced and self._refresh_wanted and self._pending_refresh is None

    @property
    def last_update_id(self) -> int:
        return self._last_id

    def _status(self, synced: bool, reason: str, ts_ns: int) -> BookSyncStatus:
        return BookSyncStatus(
            exchange=EXCHANGE,
            symbol=self.inst.symbol,
            ts_recv_ns=ts_ns,
            synced=synced,
            reason=reason,
        )

    def reset(self, reason: str, ts_ns: int, *, level: int = logging.WARNING) -> BookSyncStatus:
        was_synced = self._synced
        self._synced = False
        self._awaiting_first = False
        self._buffer.clear()
        self._history.clear()
        self._pending_validation = None
        self._refresh_wanted = False
        self._pending_refresh = None
        self._mid_bounds = None
        self._coverage = None
        self.book.clear()
        if was_synced:
            log.log(
                level, "order book resync", extra={"symbol": self.inst.symbol, "reason": reason}
            )
        return self._status(False, reason, ts_ns)

    # --- Stream -------------------------------------------------------------------------

    def on_depth(self, upd: DepthUpdate) -> list[Event]:
        if not self._synced:
            if len(self._buffer) == self._buffer.maxlen:
                # Ältestes fällt raus. Unkritisch: ist der Snapshot dann zu alt,
                # erkennt on_snapshot das (StaleSnapshotError) und es wird neu geholt.
                self.stats.buffer_overflows += 1
            self._buffer.append(upd)
            return []

        if self._awaiting_first:
            # Doku Schritt 3: Events mit u < lastUpdateId verwerfen
            if upd.final_id < self._snapshot_id:
                return []
            # Doku Schritt 4: erstes Event braucht U <= lastUpdateId <= u
            if upd.first_id > self._snapshot_id:
                self.stats.stale_snapshots += 1
                status = self.reset(
                    f"snapshot {self._snapshot_id} older than stream (U={upd.first_id})",
                    upd.recv_ns,
                    level=logging.INFO,
                )
                self._buffer.append(upd)
                return [status]
            self._awaiting_first = False
            return self._apply(upd, prev_id=self._snapshot_id)

        # Doku Schritt 5: pu muss dem u des Vorgängers entsprechen
        if upd.prev_final_id != self._last_id:
            self.stats.gaps += 1
            status = self.reset(
                f"sequence gap: pu={upd.prev_final_id}, expected {self._last_id}", upd.recv_ns
            )
            self._buffer.append(upd)
            return [status]
        return self._apply(upd, prev_id=upd.prev_final_id)

    def on_snapshot(self, snap: DepthSnapshot) -> list[Event]:
        if self._synced:
            return []
        lid = snap.last_update_id
        while self._buffer and self._buffer[0].final_id < lid:
            self._buffer.popleft()
        if self._buffer and self._buffer[0].first_id > lid:
            self.stats.stale_snapshots += 1
            raise StaleSnapshotError(
                f"snapshot {lid} older than oldest buffered event U={self._buffer[0].first_id}"
            )

        self.book.load(snap.bids, snap.asks)
        self._snapshot_id = self._last_id = lid
        self._synced = True
        self._awaiting_first = True
        self.stats.syncs += 1
        self._init_coverage(snap)

        events: list[Event] = [
            BookSnapshot(
                exchange=EXCHANGE,
                symbol=self.inst.symbol,
                ts_exchange_ns=snap.transact_time_ms * _MS if snap.transact_time_ms else None,
                ts_recv_ns=snap.recv_ns,
                bids=snap.bids,
                asks=snap.asks,
                last_seq=lid,
            ),
            self._status(True, "snapshot applied", snap.recv_ns),
        ]
        # Gepufferte Events laufen durch genau denselben Pfad wie Live-Events.
        pending = list(self._buffer)
        self._buffer.clear()
        for upd in pending:
            events.extend(self.on_depth(upd))
        log.info(
            "order book synced",
            extra={"symbol": self.inst.symbol, "snapshot_id": lid, "replayed": len(pending)},
        )
        return events

    def _apply(self, upd: DepthUpdate, prev_id: int) -> list[Event]:
        self.book.apply(upd.bids, upd.asks)
        self._last_id = upd.final_id
        self._history.append(
            _Applied(
                prev_id,
                upd.final_id,
                frozenset(lvl.price_ticks for lvl in upd.bids),
                frozenset(lvl.price_ticks for lvl in upd.asks),
            )
        )
        events: list[Event] = [
            BookDelta(
                exchange=EXCHANGE,
                symbol=self.inst.symbol,
                ts_exchange_ns=upd.transact_time_ms * _MS,
                ts_recv_ns=upd.recv_ns,
                bids=upd.bids,
                asks=upd.asks,
                first_seq=upd.first_id,
                last_seq=upd.final_id,
                prev_last_seq=upd.prev_final_id,
            )
        ]
        if self.book.is_crossed():
            self.stats.crossed += 1
            events.append(self.reset(f"crossed book after u={upd.final_id}", upd.recv_ns))
            return events
        if self._left_coverage() and not self._refresh_wanted and self._pending_refresh is None:
            self.stats.coverage_refreshes += 1
            self._refresh_wanted = True
            log.info(
                "mid near snapshot edge, requesting refresh", extra={"symbol": self.inst.symbol}
            )
        if self._pending_validation is not None:
            events.extend(self._try_validate(upd.recv_ns))
        if self._synced and self._pending_refresh is not None:
            events.extend(self._try_refresh(upd.recv_ns))
        return events

    # --- Coverage -----------------------------------------------------------------------
    # Der Snapshot enthält nur die besten N Level. Level außerhalb, die sich seit dem
    # Sync nie geändert haben, kennt das lokale Buch nicht. Läuft der Preis dorthin,
    # hätte die Heatmap Löcher -> rechtzeitig frischen Snapshot holen.

    def _init_coverage(self, snap: DepthSnapshot) -> None:
        if not snap.bids or not snap.asks:
            self._coverage = self._mid_bounds = None
            return
        lo = min(lvl.price_ticks for lvl in snap.bids)
        hi = max(lvl.price_ticks for lvl in snap.asks)
        bb = max(lvl.price_ticks for lvl in snap.bids)
        ba = min(lvl.price_ticks for lvl in snap.asks)
        mid = (bb + ba) / 2
        f = self._coverage_fraction
        self._coverage = (lo, hi)
        self._mid_bounds = (mid - f * (mid - lo), mid + f * (hi - mid))

    def _left_coverage(self) -> bool:
        if self._mid_bounds is None:
            return False
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if bb is None or ba is None:
            return False
        mid = (bb.price_ticks + ba.price_ticks) / 2
        return not (self._mid_bounds[0] <= mid <= self._mid_bounds[1])

    def _set_coverage(self, snap: DepthSnapshot) -> None:
        """Coverage = Preisbereich des Snapshots; Refresh-Grenzen um den *aktuellen* Mid."""
        if not snap.bids or not snap.asks:
            return
        lo = min(lvl.price_ticks for lvl in snap.bids)
        hi = max(lvl.price_ticks for lvl in snap.asks)
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if bb is None or ba is None:
            return
        mid = (bb.price_ticks + ba.price_ticks) / 2
        f = self._coverage_fraction
        self._coverage = (lo, hi)
        self._mid_bounds = (mid - f * (mid - lo), mid + f * (hi - mid))

    def _touched_since(self, lid: int) -> tuple[set[int], set[int]] | None:
        """Preise, die ein Delta mit ``u > lid`` geändert hat (Bids, Asks).

        ``None``, wenn die Delta-Historie nicht bis ``lid`` zurückreicht – dann lässt sich
        nicht sagen, welche Level seit ``lid`` unverändert sind.
        """
        touched_b: set[int] = set()
        touched_a: set[int] = set()
        covered = self._last_id == lid
        for h in reversed(self._history):
            if h.final_id <= lid:
                covered = True
                break
            touched_b |= h.bid_prices
            touched_a |= h.ask_prices
        else:
            covered = covered or (bool(self._history) and self._history[0].prev_id <= lid)
        return (touched_b, touched_a) if covered else None

    # --- Heißer Refresh -----------------------------------------------------------------
    # Statt das Buch beim Verlassen des Snapshot-Bereichs zu verwerfen (= Lücke in der
    # Heatmap), wird ein frischer Snapshot *eingearbeitet*: Für jeden Preis im Bereich des
    # neuen Snapshots, den seit dessen Update-ID L kein Delta berührt hat, ist der
    # Snapshot-Wert exakt der aktuelle Wert (gleiche Begründung wie bei der Validierung).
    # Berührte Preise sind lokal ohnehin schon aktuell. Das Buch bleibt durchgehend gültig.

    def on_refresh_snapshot(self, snap: DepthSnapshot) -> list[Event]:
        self._refresh_wanted = False
        if not self._synced:
            return []
        self._pending_refresh = snap
        return self._try_refresh(snap.recv_ns)

    def _try_refresh(self, ts_ns: int) -> list[Event]:
        snap = self._pending_refresh
        if snap is None or self._awaiting_first or self._last_id < snap.last_update_id:
            return []  # lokales Buch hat L noch nicht erreicht -> später erneut
        self._pending_refresh = None
        lid = snap.last_update_id
        touched = self._touched_since(lid) if lid >= self._snapshot_id else None
        if touched is None:
            # Ohne ausreichende Historie ist Einarbeiten nicht sicher -> klassischer Neu-Sync
            return [self.reset("coverage refresh not possible, resync", ts_ns, level=logging.INFO)]
        changes: dict[Side, list[PriceLevel]] = {Side.BID: [], Side.ASK: []}
        for side, remote_levels, t in (
            (Side.BID, snap.bids, touched[0]),
            (Side.ASK, snap.asks, touched[1]),
        ):
            if not remote_levels:
                continue
            remote = {lvl.price_ticks: lvl.qty for lvl in remote_levels}
            lo = min(remote) if side is Side.BID else None
            hi = max(remote) if side is Side.ASK else None
            local = self.book.side(side)
            prices = set(remote) | {
                p for p in local if (lo is None or p >= lo) and (hi is None or p <= hi)
            }
            for p in prices - t:
                want = remote.get(p, Decimal(0))
                if local.get(p, Decimal(0)) != want:
                    changes[side].append(PriceLevel(p, want))
        bids, asks = tuple(changes[Side.BID]), tuple(changes[Side.ASK])
        self.book.apply(bids, asks)
        self._set_coverage(snap)
        log.info(
            "coverage refreshed without resync",
            extra={"symbol": self.inst.symbol, "levels_changed": len(bids) + len(asks)},
        )
        if not bids and not asks:
            return []
        # Synthetisches Delta, damit Downstream-Bücher dieselben Änderungen bekommen
        return [
            BookDelta(
                exchange=EXCHANGE,
                symbol=self.inst.symbol,
                ts_exchange_ns=None,
                ts_recv_ns=ts_ns,
                bids=bids,
                asks=asks,
                first_seq=self._last_id,
                last_seq=self._last_id,
                prev_last_seq=self._last_id,
            )
        ]

    # --- Validierung --------------------------------------------------------------------

    def request_validation(self, snap: DepthSnapshot) -> list[Event]:
        """Vergleicht einen frischen REST-Snapshot mit dem lokalen Buch.

        Problem: Der Snapshot gehört zu Update-ID L, das lokale Buch ist meist schon
        weiter (REST-Latenz > 100 ms Stream-Takt). Exakt vergleichbar ist deshalb nur,
        was sich *seit L nicht geändert hat*. Dafür merken wir uns die Preise der
        zuletzt angewendeten Deltas (``_history``) und nehmen alle Preise aus dem
        Vergleich, die ein Delta mit ``u > L`` berührt hat. Für alle übrigen Preise gilt
        exakt: Menge(lokal, jetzt) == Menge(Börse, bei L). Keine Heuristik, kein
        "ungefähr gleich" – jede Abweichung ist ein echter Fehler.
        """
        if not self._synced:
            self.stats.validations_skipped += 1
            return []
        self._pending_validation = snap
        return self._try_validate(snap.recv_ns)

    def _try_validate(self, ts_ns: int) -> list[Event]:
        snap = self._pending_validation
        if snap is None or self._awaiting_first or self._last_id < snap.last_update_id:
            return []  # lokales Buch hat L noch nicht erreicht -> später erneut
        self._pending_validation = None
        lid = snap.last_update_id
        if lid < self._snapshot_id:
            self.stats.validations_skipped += 1
            return []

        touched = self._touched_since(lid)
        if touched is None:
            self.stats.validations_skipped += 1
            log.info(
                "book validation skipped: delta history too short",
                extra={"symbol": self.inst.symbol, "snapshot_id": lid},
            )
            return []
        touched_b, touched_a = touched

        result = ValidationResult(lid, self._last_id, 0, len(touched_b) + len(touched_a))
        cov_lo, cov_hi = self._coverage or (None, None)
        for side, remote_levels, side_touched in (
            (Side.BID, snap.bids, touched_b),
            (Side.ASK, snap.asks, touched_a),
        ):
            if not remote_levels:
                continue
            remote = {lvl.price_ticks: lvl.qty for lvl in remote_levels}
            # Vergleichsbereich: Preisbereich des Kontroll-Snapshots ∩ Sync-Coverage.
            # Für Bids: alles ab dem tiefsten Snapshot-Bid aufwärts; Asks spiegelbildlich.
            if side is Side.BID:
                lo = max(min(remote), cov_lo) if cov_lo is not None else min(remote)
                hi = None
            else:
                lo = None
                hi = min(max(remote), cov_hi) if cov_hi is not None else max(remote)
            local_side = self.book.side(side)
            prices = {p for p in remote if (lo is None or p >= lo) and (hi is None or p <= hi)}
            prices |= {p for p in local_side if (lo is None or p >= lo) and (hi is None or p <= hi)}
            for p in prices - side_touched:
                result.compared += 1
                local_q = local_side.get(p, Decimal(0))
                remote_q = remote.get(p, Decimal(0))
                if local_q != remote_q:
                    result.mismatches.append((side.value, p, local_q, remote_q))

        self.last_validation = result
        if result.ok:
            self.stats.validations_ok += 1
            log.info(
                "book validation ok",
                extra={
                    "symbol": self.inst.symbol,
                    "compared": result.compared,
                    "excluded_touched": result.excluded_touched,
                    "lag_ids": self._last_id - lid,
                },
            )
            return []
        self.stats.validations_failed += 1
        log.warning(
            "book validation MISMATCH",
            extra={
                "symbol": self.inst.symbol,
                "mismatches": len(result.mismatches),
                "compared": result.compared,
                "examples": [
                    (s, str(self.inst.ticks_to_price(p)), str(lq), str(rq))
                    for s, p, lq, rq in result.mismatches[:5]
                ],
            },
        )
        if self._resync_on_mismatch:
            return [self.reset("validation mismatch", ts_ns)]
        return []


# ======================================================================================
# Adapter (I/O)
# ======================================================================================


class BinanceUsdmAdapter:
    """Live-Datenquelle für ein Symbol. Publiziert normalisierte Events + Rohdaten."""

    def __init__(self, cfg: BinanceUsdmConfig, symbol: str, bus: EventBus) -> None:
        self.cfg = cfg
        self.symbol = symbol.upper()
        self.name = f"{EXCHANGE}:{self.symbol}"
        self.bus = bus
        self.inst: Instrument | None = None
        self.sync: BinanceDepthSync | None = None
        self._stop = asyncio.Event()
        self._need_snapshot = asyncio.Event()
        self._last_refresh = 0.0
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Serialisiert "Zustand ändern + Events publizieren". Ohne Lock könnte der
        # Snapshot-Task beim await auf publish() unterbrochen werden, der Stream-Task
        # ein neueres Delta *vor* den gepufferten Deltas publizieren -> Downstream-
        # Bücher bekämen Events in falscher Reihenfolge.
        self._lock = asyncio.Lock()

    def stop(self) -> None:
        self._stop.set()

    async def _publish(self, events: list[Event]) -> None:
        for ev in events:
            await self.bus.publish(ev)

    def _raw(self, stream: str, payload: str, recv_ns: int) -> RawMessage:
        return RawMessage(
            exchange=EXCHANGE,
            symbol=self.symbol,
            ts_recv_ns=recv_ns,
            stream=stream,
            payload=payload,
        )

    def _meta(self, event: str, recv_ns: int, **kw: str) -> RawMessage:
        return self._raw("meta", json.dumps({"event": event, **kw}), recv_ns)

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data, _ = await get_json(session, f"{self.cfg.rest_url}/fapi/v1/exchangeInfo")
            self.inst = inst = parse_instrument(data, self.symbol)
            sym_entry = next(s for s in data["symbols"] if s["symbol"] == self.symbol)
            tradfi = is_tradfi(sym_entry, self.cfg.tradfi_categories)
            reconnect = (
                self.cfg.reconnect.model_copy(
                    update={"stale_timeout_s": self.cfg.tradfi_stale_timeout_s}
                )
                if tradfi
                else self.cfg.reconnect
            )
            now = time.time_ns()
            await self._publish(
                [
                    self._raw("rest:exchangeInfo", json.dumps(sym_entry), now),
                    instrument_info(sym_entry, inst, now, self.cfg.tradfi_categories),
                ]
            )
            self.sync = BinanceDepthSync(
                inst,
                max_buffer=self.cfg.max_buffered_updates,
                history=self.cfg.validation.history_updates,
                resync_on_mismatch=self.cfg.validation.resync_on_mismatch,
                coverage_refresh_fraction=self.cfg.coverage_refresh_fraction,
            )
            depth = depth_stream_name(self.symbol, self.cfg.depth_update_ms)
            sym = self.symbol.lower()
            market_streams = [
                f"{sym}@aggTrade",
                f"{sym}@kline_1m",
            ]
            if not self.cfg.liquidations_all_market:
                market_streams.append(f"{sym}@forceOrder")
            log.info("adapter starting", extra={"symbol": self.symbol, "tick": str(inst.tick_size)})
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    run_ws(
                        session,
                        f"{self.cfg.ws_public_url}/stream?streams={depth}",
                        name=f"{self.name}:public",
                        cfg=reconnect,
                        stop=self._stop,
                        on_message=self._on_public,
                        on_open=self._on_public_open,
                        on_close=self._on_public_close,
                    )
                )
                tg.create_task(
                    run_ws(
                        session,
                        f"{self.cfg.ws_market_url}/stream?streams={'/'.join(market_streams)}",
                        name=f"{self.name}:market",
                        cfg=reconnect,
                        stop=self._stop,
                        on_message=self._on_market,
                        on_open=lambda: self._on_market_open(session),
                        on_close=lambda r: self._on_meta("disconnect", "market", r),
                    )
                )
                tg.create_task(self._supervise("snapshot", lambda: self._snapshot_worker(session)))
                if self.cfg.validation.enabled:
                    tg.create_task(
                        self._supervise("validation", lambda: self._validation_worker(session))
                    )
                if self.cfg.metrics.enabled:
                    tg.create_task(
                        self._supervise("open_interest", lambda: self._oi_worker(session))
                    )
                    tg.create_task(self._supervise("ratios", lambda: self._ratios_worker(session)))
        log.info("adapter stopped", extra={"symbol": self.symbol})

    async def _supervise(self, name: str, worker: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Startet einen Worker nach unerwartetem Fehler neu, statt den ganzen Adapter
        (TaskGroup) mitzureißen. Abbruch (CancelledError) wird durchgereicht."""
        while not self._stop.is_set():
            try:
                await worker()
                return
            except Exception:
                log.exception("adapter worker crashed, restarting", extra={"worker": name})
                if await sleep_or_stop(self._stop, 5.0):
                    return
                self._need_snapshot.set()

    # --- Public (Depth) -----------------------------------------------------------------

    async def _on_meta(self, event: str, conn: str, reason: str = "") -> None:
        if self.bus.closed:  # Herunterfahren nach Fehler: Bus schon zu
            return
        async with self._lock:
            await self.bus.publish(self._meta(event, time.time_ns(), conn=conn, reason=reason))

    async def _on_public_open(self) -> None:
        await self._on_meta("connect", "public")

    async def _on_public_close(self, reason: str) -> None:
        assert self.sync is not None
        if self.bus.closed:  # Herunterfahren nach Fehler: Bus schon zu
            return
        async with self._lock:
            now = time.time_ns()
            await self.bus.publish(self._meta("disconnect", now, conn="public", reason=reason))
            # Nach Reconnect vollständiger Neu-Sync (Stream-Kontinuität ist verloren).
            shutdown = self._stop.is_set() or reason == "cancelled"
            level = logging.INFO if shutdown else logging.WARNING
            await self.bus.publish(self.sync.reset(f"disconnected: {reason}", now, level=level))

    async def _on_public(self, text: str, recv_ns: int) -> None:
        assert self.sync is not None
        assert self.inst is not None
        _, data = unwrap_combined(text)
        if data.get("e") != "depthUpdate":
            return
        async with self._lock:
            await self.bus.publish(self._raw("depth", text, recv_ns))
            events = self.sync.on_depth(parse_depth_update(data, self.inst, recv_ns))
            await self._publish(events)
            if self.sync.wants_snapshot or self.sync.wants_refresh:
                self._need_snapshot.set()

    async def _snapshot_worker(self, session: aiohttp.ClientSession) -> None:
        assert self.sync is not None
        assert self.inst is not None
        url = f"{self.cfg.rest_url}/fapi/v1/depth"
        params: dict[str, str | int] = {"symbol": self.symbol, "limit": self.cfg.snapshot_limit}
        stale_delay = 0.25
        while not self._stop.is_set():
            waiter = asyncio.ensure_future(self._need_snapshot.wait())
            stopper = asyncio.ensure_future(self._stop.wait())
            await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            stopper.cancel()
            if self._stop.is_set():
                return
            self._need_snapshot.clear()
            if self.sync.synced:
                if self.sync.wants_refresh:
                    await self._refresh(session, url, params)
                continue
            try:
                data, text = await get_json(session, url, params)
            except RestError as e:
                log.error("depth snapshot failed", extra={"symbol": self.symbol, "error": str(e)})
                if await sleep_or_stop(self._stop, 5.0):
                    return
                self._need_snapshot.set()
                continue
            recv_ns = time.time_ns()
            async with self._lock:
                await self.bus.publish(self._raw("rest:depth", text, recv_ns))
                try:
                    events = self.sync.on_snapshot(parse_depth_snapshot(data, self.inst, recv_ns))
                except StaleSnapshotError as e:
                    log.info("stale snapshot, refetching", extra={"reason": str(e)})
                    events = []
                    retry = True
                else:
                    retry = self.sync.wants_snapshot
                    stale_delay = 0.25
                await self._publish(events)
            if retry:
                if await sleep_or_stop(self._stop, stale_delay):
                    return
                stale_delay = min(stale_delay * 2, 5.0)
                self._need_snapshot.set()

    async def _refresh(
        self, session: aiohttp.ClientSession, url: str, params: dict[str, str | int]
    ) -> None:
        """Heißer Coverage-Refresh (siehe ``BinanceDepthSync.on_refresh_snapshot``)."""
        assert self.sync is not None
        assert self.inst is not None
        wait = self._last_refresh + self.cfg.coverage_refresh_min_interval_s - time.monotonic()
        if wait > 0 and await sleep_or_stop(self._stop, wait):
            return
        self._last_refresh = time.monotonic()
        try:
            data, text = await get_json(session, url, params)
        except RestError as e:
            log.warning("refresh snapshot failed", extra={"symbol": self.symbol, "error": str(e)})
            self._need_snapshot.set()
            return
        recv_ns = time.time_ns()
        async with self._lock:
            await self.bus.publish(self._raw("rest:depth:refresh", text, recv_ns))
            events = self.sync.on_refresh_snapshot(parse_depth_snapshot(data, self.inst, recv_ns))
            await self._publish(events)
            if self.sync.wants_snapshot or self.sync.wants_refresh:
                self._need_snapshot.set()

    async def _validation_worker(self, session: aiohttp.ClientSession) -> None:
        assert self.sync is not None
        assert self.inst is not None
        url = f"{self.cfg.rest_url}/fapi/v1/depth"
        params = {"symbol": self.symbol, "limit": self.cfg.validation.snapshot_limit}
        while not await sleep_or_stop(self._stop, self.cfg.validation.interval_s):
            if not self.sync.synced:
                continue
            try:
                data, text = await get_json(session, url, params)
            except RestError as e:
                log.warning("validation snapshot failed", extra={"error": str(e)})
                continue
            recv_ns = time.time_ns()
            async with self._lock:
                await self.bus.publish(self._raw("rest:depth:validate", text, recv_ns))
                events = self.sync.request_validation(
                    parse_depth_snapshot(data, self.inst, recv_ns)
                )
                await self._publish(events)
                if self.sync.wants_snapshot:
                    self._need_snapshot.set()

    # --- Market (Trades, Liquidationen) -------------------------------------------------

    async def _on_market(self, text: str, recv_ns: int) -> None:
        assert self.inst is not None
        _, data = unwrap_combined(text)
        etype = data.get("e")
        if etype == "aggTrade":
            ev: Event = parse_agg_trade(data, self.inst, recv_ns)
            kind = "aggTrade"
        elif etype == "forceOrder":
            ev = parse_force_order(data, self.inst, recv_ns)
            kind = "forceOrder"
        elif etype == "markPriceUpdate":
            ev = parse_mark_price(data, self.symbol, recv_ns)
            kind = "markPrice"
        elif etype == "24hrMiniTicker":
            ev = parse_mini_ticker(data, self.symbol, recv_ns)
            kind = "miniTicker"
        elif etype == "kline":
            ev = parse_kline_ws(data, self.symbol, recv_ns)
            kind = "kline"
        else:
            return
        async with self._lock:
            await self.bus.publish(self._raw(kind, text, recv_ns))
            await self.bus.publish(ev)

    async def _on_market_open(self, session: aiohttp.ClientSession) -> None:
        await self._on_meta("connect", "market")
        # 1m-Historie bei jedem (Re)Connect neu laden: schließt Lücken während der
        # Trennung. Im Hintergrund, damit der Empfang nicht wartet.
        task = asyncio.create_task(self._load_candles(session))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _load_candles(self, session: aiohttp.ClientSession) -> None:
        url = f"{self.cfg.rest_url}/fapi/v1/klines"
        params: dict[str, str | int] = {
            "symbol": self.symbol,
            "interval": "1m",
            "limit": self.cfg.candles_1m_history,
        }
        try:
            rows, text = await get_json(session, url, params, attempts=3)
        except RestError as e:
            log.warning("candle history failed", extra={"symbol": self.symbol, "error": str(e)})
            return
        now = time.time_ns()
        ev = CandleHistory(
            exchange=EXCHANGE,
            symbol=self.symbol,
            ts_recv_ns=now,
            interval="1m",
            source="futures",
            bars=tuple(parse_kline_row(r) for r in rows),
        )
        await self._publish_rest("rest:klines:1m", text, ev, now)

    # --- Kennzahlen per REST ------------------------------------------------------------

    async def _publish_rest(self, stream: str, text: str, ev: Event | None, recv_ns: int) -> None:
        async with self._lock:
            await self.bus.publish(self._raw(stream, text, recv_ns))
            if ev is not None:
                await self.bus.publish(ev)

    async def _oi_worker(self, session: aiohttp.ClientSession) -> None:
        url = f"{self.cfg.rest_url}/fapi/v1/openInterest"
        while True:
            try:
                data, text = await get_json(session, url, {"symbol": self.symbol}, attempts=2)
            except RestError as e:
                log.warning("open interest failed", extra={"symbol": self.symbol, "error": str(e)})
            else:
                now = time.time_ns()
                await self._publish_rest(
                    "rest:openInterest", text, parse_open_interest(data, now), now
                )
            if await sleep_or_stop(self._stop, self.cfg.metrics.open_interest_interval_s):
                return

    async def _ratios_worker(self, session: aiohttp.ClientSession) -> None:
        period = self.cfg.metrics.ratio_period
        params: dict[str, str | int] = {"symbol": self.symbol, "period": period, "limit": 1}
        endpoints = (
            ("globalLongShortAccountRatio", parse_long_short),
            ("takerlongshortRatio", parse_taker_volume),
        )
        while True:
            for name, parser in endpoints:
                try:
                    data, text = await get_json(
                        session, f"{self.cfg.rest_url}/futures/data/{name}", params, attempts=2
                    )
                except RestError as e:
                    log.warning(
                        "ratio request failed",
                        extra={"symbol": self.symbol, "endpoint": name, "error": str(e)},
                    )
                    continue
                now = time.time_ns()
                await self._publish_rest(
                    f"rest:{name}", text, parser(data, self.symbol, period, now), now
                )
            if await sleep_or_stop(self._stop, self.cfg.metrics.ratios_interval_s):
                return


class BinanceLiquidationStream:
    """Liquidationen *aller* USDⓈ-M-Symbole über ``!forceOrder@arr`` (/market).

    Laut Doku pro Symbol höchstens die letzte Liquidation je 1000 ms; der Stream
    aktualisiert also sekündlich über den ganzen Markt. Seit der "CM-Migration" enthält er
    auch Coin-margined Kontrakte (``st = 2``, Menge in Kontrakten statt Basis-Währung) –
    die werden verworfen, sonst wären die USDT-Summen falsch.
    """

    name = f"{EXCHANGE}:liquidations"

    def __init__(self, cfg: BinanceUsdmConfig, bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.instruments: dict[str, Instrument] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data, _ = await get_json(session, f"{self.cfg.rest_url}/fapi/v1/exchangeInfo")
            self.instruments = parse_instruments(data)
            log.info("liquidation stream starting", extra={"symbols": len(self.instruments)})
            await run_ws(
                session,
                f"{self.cfg.ws_market_url}/stream?streams=!forceOrder@arr",
                name=self.name,
                cfg=self.cfg.reconnect,
                stop=self._stop,
                on_message=self._on_message,
            )

    async def _on_message(self, text: str, recv_ns: int) -> None:
        _, data = unwrap_combined(text)
        items: list[dict[str, Any]] = data if isinstance(data, list) else [data]
        events: list[Event] = [
            RawMessage(
                exchange=EXCHANGE,
                symbol="ALL",
                ts_recv_ns=recv_ns,
                stream="forceOrder@arr",
                payload=text,
            )
        ]
        for item in items:
            if item.get("e") != "forceOrder" or item.get("st", 1) != 1:
                continue
            inst = self.instruments.get(item["o"]["s"])
            if inst is None:  # z. B. neu gelistet seit dem Start
                continue
            events.append(parse_force_order(item, inst, recv_ns))
        for ev in events:
            await self.bus.publish(ev)


class BinanceMarketStats:
    """Kennzahlen für *alle* konfigurierten Coins über eine Verbindung (/market):

    - ``!miniTicker@arr`` – 24h-Statistik, sekündlich, nur geänderte Symbole
    - ``!markPrice@arr@1s`` – Mark-/Indexpreis + Funding (TradFi kommt laut Doku in
      eigenen Nachrichten, ist aber dasselbe Format)
    - ``tradingSession`` – Handelszeiten der TradFi-Märkte (z. B. Rohstoffe)

    Doku (alle drei Streams, inkl. ``st``-Feld nach der CM-Migration):
    https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market
    TradFi-Kontrakte: exchangeInfo ``contractType = TRADIFI_PERPETUAL``.

    Beim Start: exchangeInfo prüfen, welche konfigurierten Symbole es gibt, und für jedes
    ein ``InstrumentInfo`` publizieren – damit kennt das Frontend alle Coins samt
    Tick-Größe, auch wenn deren Orderbuch (noch) nicht läuft.
    """

    name = f"{EXCHANGE}:stats"

    def __init__(
        self, cfg: BinanceUsdmConfig, symbols: list[str], bus: EventBus, *, discover: bool = True
    ) -> None:
        self.cfg = cfg
        self.wanted = [s.upper() for s in symbols]
        self.discover = discover
        self.discovered: list[str] = []
        self.bus = bus
        self.available: dict[str, Instrument] = {}
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data, _ = await get_json(session, f"{self.cfg.rest_url}/fapi/v1/exchangeInfo")
            entries = {s["symbol"]: s for s in data["symbols"]}
            instruments = parse_instruments(data)
            now = time.time_ns()
            if self.cfg.tradfi_auto_discover and self.discover:
                known = set(self.wanted)
                found = sorted(
                    sym
                    for sym, e in entries.items()
                    if is_tradfi(e, self.cfg.tradfi_categories)
                    and e.get("status") == "TRADING"
                    and sym not in known
                )
                self.discovered = found
                # Zur Diagnose: welche Kontrakt-/Underlying-Typen liefert die Börse?
                types = Counter(
                    f"{e.get('contractType', '?')}/{e.get('underlyingType', '?')}"
                    for e in entries.values()
                    if e.get("status") == "TRADING"
                )
                log.info("exchangeInfo contract types", extra={"types": dict(types)})
                missing = sorted(
                    s
                    for s in self.cfg.tradfi_categories
                    if s not in entries or entries[s].get("status") != "TRADING"
                )
                if missing:
                    log.info("configured tradfi symbols not trading", extra={"symbols": missing})
                self.wanted = self.wanted + found
                log.info("tradfi contracts discovered", extra={"count": len(found)})
            for sym in self.wanted:
                entry = entries.get(sym)
                if entry is None or sym not in instruments or entry.get("status") != "TRADING":
                    log.warning(
                        "symbol not available on Binance USDⓈ-M, skipped",
                        extra={"symbol": sym, "status": entry.get("status") if entry else None},
                    )
                    continue
                inst = instruments[sym]
                self.available[sym] = inst
                await self.bus.publish(
                    instrument_info(entry, inst, now, self.cfg.tradfi_categories)
                )
            self.ready.set()
            log.info(
                "market stats starting",
                extra={"available": len(self.available), "configured": len(self.wanted)},
            )
            await run_ws(
                session,
                f"{self.cfg.ws_market_url}/stream?streams=!miniTicker@arr/!markPrice@arr@1s/tradingSession",
                name=self.name,
                cfg=self.cfg.reconnect,
                stop=self._stop,
                on_message=self._on_message,
            )

    async def _on_message(self, text: str, recv_ns: int) -> None:
        _, data = unwrap_combined(text)
        items: list[dict[str, Any]] = data if isinstance(data, list) else [data]
        for d in items:
            if d.get("st", 1) != 1:  # Coin-margined (CM) überspringen
                continue
            etype = d.get("e")
            sym = d.get("s")
            ev: Event | None = None
            if etype == "24hrMiniTicker" and sym in self.available:
                ev = parse_mini_ticker(d, sym, recv_ns)
            elif etype == "markPriceUpdate" and sym in self.available:
                ev = parse_mark_price(d, sym, recv_ns)
            elif isinstance(etype, str) and etype.endswith("Update") and "S" in d:
                # tradingSession: EquityUpdate, CommodityUpdate, FXUpdate, …
                ev = MarketSession(
                    exchange=EXCHANGE,
                    symbol=etype.removesuffix("Update").upper(),
                    ts_exchange_ns=_ms(d.get("E")),
                    ts_recv_ns=recv_ns,
                    session=str(d["S"]),
                    start_ns=_ms(d.get("t")),
                    end_ns=_ms(d.get("T")),
                )
            if ev is not None:
                await self.bus.publish(ev)
