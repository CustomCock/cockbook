"""Normalisierte Markt-Events.

Jeder Adapter übersetzt börsenspezifische Nachrichten in diese Typen. Alles hinter
dem Adapter (Orderbuch, Indikatoren, Server) kennt nur diese Klassen und weiß nicht,
von welcher Börse oder ob live/Replay die Daten kommen.

Konventionen (siehe CLAUDE.md):
- Preise sind ``int`` in Tick-Einheiten des Symbols (``price_ticks``). Nie ``float``,
  weil Floats als Dict-Keys durch Rundung zu zwei "verschiedenen" Leveln für
  denselben Preis führen können (0.1 + 0.2 != 0.3).
- Mengen sind ``Decimal`` – exakt, und Börsen liefern sie ohnehin als String.
- Zeitstempel sind ``int`` Nanosekunden seit Unix-Epoch. ``ts_exchange_ns`` ist die
  Zeit laut Börse (falls geliefert), ``ts_recv_ns`` unser lokaler Empfangszeitpunkt.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal


class Side(enum.Enum):
    BID = "bid"
    ASK = "ask"

    @property
    def opposite(self) -> Side:
        return Side.ASK if self is Side.BID else Side.BID


class L3Action(enum.Enum):
    """Lebenszyklus einer Einzelorder im Level-3-Feed."""

    OPEN = "open"  # neue Order liegt im Buch
    CHANGE = "change"  # Größe geändert (z. B. Teil-Cancel)
    MATCH = "match"  # (teilweise) gefüllt
    DONE = "done"  # Order hat das Buch verlassen (gefüllt oder gecancelt)


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Basisklasse. Subscriber auf ``Event`` bekommen alle Event-Typen."""

    exchange: str
    symbol: str
    ts_recv_ns: int
    ts_exchange_ns: int | None = None

    @property
    def ts_ns(self) -> int:
        """Kanonische Event-Zeit: Börsenzeit, falls vorhanden, sonst Empfangszeit."""
        return self.ts_exchange_ns if self.ts_exchange_ns is not None else self.ts_recv_ns


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price_ticks: int
    qty: Decimal  # 0 bedeutet in einem Delta: Level entfernen


@dataclass(frozen=True, slots=True, kw_only=True)
class BookSnapshot(Event):
    """Vollständiger Zustand des (Teil-)Orderbuchs zu einem Zeitpunkt."""

    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    # Sequenznummer/Update-ID, ab der Deltas angewendet werden dürfen.
    last_seq: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BookDelta(Event):
    """Inkrementelle Änderung. Level mit ``qty == 0`` werden gelöscht.

    Die Sequenzfelder sind generisch gehalten; jeder Adapter mappt seine Börse darauf
    (z. B. Binance: ``U`` -> first_seq, ``u`` -> last_seq, ``pu`` -> prev_last_seq).
    """

    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    first_seq: int
    last_seq: int
    prev_last_seq: int | None = None
    checksum: int | None = None  # nur wo die Börse eine liefert (z. B. OKX)


@dataclass(frozen=True, slots=True, kw_only=True)
class Trade(Event):
    price_ticks: int
    qty: Decimal
    # Seite des Takers: BID = aggressiver Käufer (hebt den Ask), ASK = aggressiver Verkäufer.
    aggressor: Side
    trade_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Liquidation(Event):
    price_ticks: int  # auf Ticks gerundet (Durchschnittspreise liegen oft zwischen Ticks)
    price: Decimal  # exakter Ausführungspreis – für Anzeige und USDT-Gegenwert
    qty: Decimal
    # Seite der Liquidations-*Order*: ASK = Long wird liquidiert (Zwangsverkauf).
    side: Side


@dataclass(frozen=True, slots=True, kw_only=True)
class L3Order(Event):
    """Einzelne Order mit ID – nur bei Börsen mit öffentlichem Level-3-Feed."""

    order_id: str
    action: L3Action
    side: Side
    price_ticks: int | None  # bei DONE nicht immer geliefert
    qty: Decimal  # verbleibende Größe nach dem Ereignis


MARKET_EVENT_TYPES: tuple[type[Event], ...] = (
    BookSnapshot,
    BookDelta,
    Trade,
    Liquidation,
    L3Order,
)


# --- Infrastruktur-Events -------------------------------------------------------------
# Keine Marktdaten im engeren Sinn, laufen aber über denselben Bus, damit Recorder,
# Replay und Frontend nichts über einzelne Adapter wissen müssen.


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentInfo(Event):
    """Handelsregeln eines Symbols. Wird vom Adapter vor allen Marktdaten publiziert."""

    tick_size: Decimal
    step_size: Decimal
    # "crypto" oder "tradfi" (Rohstoffe/Aktien mit Handelszeiten)
    kind: str = "crypto"
    # Feinere Einordnung: crypto | stock | etf | commodity | fx | tradfi
    category: str = "crypto"
    # TradFi: Markt der Handelszeiten (Name aus dem tradingSession-Stream, z. B. EQUITY)
    market: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BookSyncStatus(Event):
    """Orderbuch-Sync-Zustand. ``synced=False`` heißt: lokales Buch ungültig bis zum
    nächsten ``BookSnapshot`` (Lücke, Reconnect, Validierungsfehler …)."""

    synced: bool
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RawMessage(Event):
    """Unveränderte Rohnachricht der Börse – Futter für Recorder und später Replay.

    ``stream`` benennt die Quelle (z. B. ``depth``, ``aggTrade``, ``rest:depth``,
    ``meta``). ``payload`` ist exakt der empfangene JSON-Text, damit ein Replay durch
    *dieselbe* Parse- und Sync-Logik laufen kann wie live.
    """

    stream: str
    payload: str


# --- Marktkennzahlen (Derivate) ------------------------------------------------------
# Börsenunabhängig normalisiert; jede Futures-Börse kann sie liefern (Phase 5).


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkPrice(Event):
    mark_price: Decimal
    index_price: Decimal | None
    funding_rate: Decimal  # pro Funding-Intervall, z. B. 0.0001 = 0.01 %
    next_funding_ns: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Ticker24h(Event):
    last: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal  # Basis-Währung
    quote_volume: Decimal  # Quote-Währung (USDT)


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenInterest(Event):
    open_interest: Decimal  # offene Kontrakte in Basis-Währung


@dataclass(frozen=True, slots=True, kw_only=True)
class LongShortRatio(Event):
    """Anteil Accounts mit Netto-Long vs. Netto-Short (alle Nutzer der Börse)."""

    period: str
    long_share: Decimal  # 0..1
    short_share: Decimal
    ratio: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class TakerVolume(Event):
    """Aggressives Kauf- vs. Verkaufsvolumen über eine Periode."""

    period: str
    buy_volume: Decimal
    sell_volume: Decimal
    ratio: Decimal


# --- Kerzen ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandleBar:
    open_time_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # Basis-Währung


@dataclass(frozen=True, slots=True, kw_only=True)
class Candle(Event):
    """Laufende oder abgeschlossene Kerze (Live-Stream)."""

    interval: str  # z. B. "1m"
    bar: CandleBar
    closed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleHistory(Event):
    """Historie als Block (REST). ``source`` z. B. "futures" (1m) oder "spot" (1d)."""

    interval: str
    source: str
    bars: tuple[CandleBar, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketSession(Event):
    """Handelszeit eines TradFi-Marktes (``symbol`` = Markt, z. B. "COMMODITY")."""

    session: str  # z. B. "REGULAR", "NO_TRADING"
    start_ns: int | None
    end_ns: int | None
