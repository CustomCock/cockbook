"""Heatmap-Aggregation: Orderbuch -> eine Spalte pro Zeitintervall.

Eine Spalte ist die Liquidität pro Preis-Bucket in einem Fenster um den Mid, plus
BBO und die im Intervall gehandelten Trades.

Designentscheidungen
--------------------
- *Zustand am Intervallende* statt Maximum/Zeitmittel über das Intervall: Bei 100 ms
  Spalten und 100 ms Depth-Takt ist das praktisch jedes Update; ein Maximum würde
  kurz aufblitzende Orders (Spoofing-Kandidaten!) künstlich verlängern. Für gröbere
  Intervalle kann man später ein zeitgewichtetes Mittel ergänzen.
- *Bucket = floor(price_ticks / bucket_ticks)*, rein ganzzahlig. Erst der fertige
  Bucket-Wert wird zu float32 – das ist Anzeige, kein Orderbuch-Key.
- *Fenster um den Mid* statt fester Preisspanne: Die Spalte wandert mit dem Preis,
  das Frontend platziert sie anhand von ``base_bucket`` absolut.
- *Trades je (Seite, Bucket) zusammengefasst* mit VWAP in Ticks: aus hunderten Fills
  pro Sekunde werden wenige Bubbles; der Preis bleibt exakt (VWAP), nur die Gruppierung
  nutzt den Bucket.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field

from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    Event,
    InstrumentInfo,
    Side,
    Trade,
)
from cockbook.core.orderbook import OrderBook


@dataclass(slots=True)
class TradeBin:
    side: Side  # Aggressor
    qty: float = 0.0
    notional_ticks: float = 0.0  # Summe price_ticks * qty -> VWAP
    count: int = 0

    @property
    def vwap_ticks(self) -> float:
        return self.notional_ticks / self.qty if self.qty else 0.0


@dataclass(slots=True)
class HeatmapColumn:
    ts_ns: int
    synced: bool
    base_bucket: int
    values: array[float]  # float32, Index = bucket - base_bucket
    best_bid: int | None
    best_ask: int | None
    trades: list[TradeBin] = field(default_factory=list)


class HeatmapAggregator:
    def __init__(self, *, bucket_ticks: int, half_range_buckets: int) -> None:
        if bucket_ticks <= 0 or half_range_buckets <= 0:
            raise ValueError("bucket_ticks und half_range_buckets müssen > 0 sein")
        self.bucket_ticks = bucket_ticks
        self.half_range = half_range_buckets
        self.book = OrderBook()
        self.synced = False
        self.instrument: InstrumentInfo | None = None
        self._trades: dict[tuple[Side, int], TradeBin] = {}

    def bucket(self, price_ticks: int) -> int:
        return price_ticks // self.bucket_ticks

    # --- Eingang ------------------------------------------------------------------------

    def on_event(self, e: Event) -> None:
        if isinstance(e, BookSnapshot | BookDelta):
            self.book.apply_event(e)
        elif isinstance(e, BookSyncStatus):
            self.synced = e.synced
            if not e.synced:
                self.book.clear()
        elif isinstance(e, Trade):
            key = (e.aggressor, self.bucket(e.price_ticks))
            b = self._trades.get(key)
            if b is None:
                b = self._trades[key] = TradeBin(e.aggressor)
            q = float(e.qty)
            b.qty += q
            b.notional_ticks += e.price_ticks * q
            b.count += 1
        elif isinstance(e, InstrumentInfo):
            self.instrument = e

    # --- Ausgang ------------------------------------------------------------------------

    def make_column(self, ts_ns: int) -> HeatmapColumn:
        trades = list(self._trades.values())
        self._trades = {}
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if not self.synced or bb is None or ba is None:
            return HeatmapColumn(ts_ns, False, 0, array("f"), None, None, trades)

        n = 2 * self.half_range
        center = self.bucket((bb.price_ticks + ba.price_ticks) // 2)
        base = center - self.half_range
        lo_ticks = base * self.bucket_ticks
        hi_ticks = (base + n) * self.bucket_ticks - 1
        values = array("f", bytes(4 * n))
        bt = self.bucket_ticks
        for side in (Side.BID, Side.ASK):
            for lvl in self.book.range(side, lo_ticks, hi_ticks):
                values[lvl.price_ticks // bt - base] += float(lvl.qty)
        return HeatmapColumn(ts_ns, True, base, values, bb.price_ticks, ba.price_ticks, trades)
