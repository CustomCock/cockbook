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
- *Das ganze bekannte Buch* (begrenzt auf ``half_range_buckets`` je Seite um den Mid,
  auf den tatsächlich belegten Bereich zugeschnitten) statt eines festen Fensters um den
  Mid: Ein Fenster hat Ränder, die mit dem Kurs mitlaufen – im Chart sah es dann so aus,
  als würde die Liquidität dem Preis folgen. Das Frontend platziert jede Spalte anhand
  von ``base_bucket`` absolut; Spalten haben daher unterschiedliche Längen.
- *Trades je (Seite, Bucket) zusammengefasst* mit VWAP in Ticks: aus hunderten Fills
  pro Sekunde werden wenige Bubbles; der Preis bleibt exakt (VWAP), nur die Gruppierung
  nutzt den Bucket.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, field
from decimal import Decimal

from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    Event,
    InstrumentInfo,
    Liquidation,
    Side,
    Trade,
)
from cockbook.core.orderbook import OrderBook


def nice_ticks(raw: float) -> int:
    """Rundet auf die (logarithmisch) nächste "runde" Zahl 1, 2, 5, 10, 20, 50, … (min. 1)."""
    if raw <= 1:
        return 1
    exp = math.floor(math.log10(raw))
    best = 1
    for m in (1, 2, 5, 10):
        cand = m * 10**exp
        if abs(math.log(cand / raw)) < abs(math.log(best / raw)):
            best = cand
    return int(best)


def auto_bucket_ticks(mid_ticks: int, tick_size: Decimal, bucket_bps: float) -> int:
    """Bucket-Größe in Ticks, sodass ein Bucket ~ ``bucket_bps`` Basispunkte des Preises ist.

    Warum relativ zum Preis statt fester Tick-Zahl? Tick-Größen sind je Symbol sehr
    verschieden (BTC 0.10 bei ~80 000 = 0.01 bp, XRP 0.0001 bei ~2 = 0.5 bp). Mit festen
    Ticks wäre BTC extrem fein und XRP grob. In Basispunkten sieht jede Heatmap gleich
    "dicht" aus und das Fenster deckt einen ähnlichen Prozentbereich ab.
    """
    mid_price = float(mid_ticks * tick_size)
    return nice_ticks(mid_price * bucket_bps / 10_000 / float(tick_size))


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
    # (Seite der Liquidations-Order, Preis in Ticks, Menge) – ASK = Long liquidiert
    liquidations: list[tuple[Side, int, float]] = field(default_factory=list)


class HeatmapAggregator:
    """``bucket_ticks=None``: Bucket wird beim ersten Snapshot aus ``bucket_bps`` bestimmt
    und bleibt dann fest (eine wechselnde Bucket-Größe würde die Historie unbrauchbar
    machen)."""

    def __init__(
        self,
        *,
        bucket_ticks: int | None,
        half_range_buckets: int,
        bucket_bps: float = 0.06,
    ) -> None:
        if (bucket_ticks is not None and bucket_ticks <= 0) or half_range_buckets <= 0:
            raise ValueError("bucket_ticks und half_range_buckets müssen > 0 sein")
        self._bucket_ticks = bucket_ticks
        self.bucket_bps = bucket_bps
        self.half_range = half_range_buckets
        self.book = OrderBook()
        self.synced = False
        self.instrument: InstrumentInfo | None = None
        self._trades: dict[tuple[Side, int], TradeBin] = {}
        self._liqs: list[tuple[Side, int, float]] = []

    @property
    def bucket_ticks(self) -> int | None:
        return self._bucket_ticks

    @property
    def ready(self) -> bool:
        """Instrument und Bucket-Größe bekannt -> Spalten können erzeugt werden."""
        return self.instrument is not None and self._bucket_ticks is not None

    def bucket(self, price_ticks: int) -> int:
        assert self._bucket_ticks is not None
        return price_ticks // self._bucket_ticks

    # --- Eingang ------------------------------------------------------------------------

    def on_event(self, e: Event) -> None:
        if isinstance(e, BookSnapshot | BookDelta):
            self.book.apply_event(e)
            if self._bucket_ticks is None and self.instrument is not None:
                bb, ba = self.book.best_bid(), self.book.best_ask()
                if bb is not None and ba is not None:
                    mid = (bb.price_ticks + ba.price_ticks) // 2
                    self._bucket_ticks = auto_bucket_ticks(
                        mid, self.instrument.tick_size, self.bucket_bps
                    )
        elif isinstance(e, BookSyncStatus):
            self.synced = e.synced
            if not e.synced:
                self.book.clear()
        elif isinstance(e, Trade):
            if self._bucket_ticks is None:
                return
            key = (e.aggressor, self.bucket(e.price_ticks))
            b = self._trades.get(key)
            if b is None:
                b = self._trades[key] = TradeBin(e.aggressor)
            q = float(e.qty)
            b.qty += q
            b.notional_ticks += e.price_ticks * q
            b.count += 1
        elif isinstance(e, Liquidation):
            self._liqs.append((e.side, e.price_ticks, float(e.qty)))
        elif isinstance(e, InstrumentInfo):
            self.instrument = e

    # --- Ausgang ------------------------------------------------------------------------

    def make_column(self, ts_ns: int) -> HeatmapColumn:
        trades = list(self._trades.values())
        self._trades = {}
        liqs, self._liqs = self._liqs, []
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if not self.synced or bb is None or ba is None or self._bucket_ticks is None:
            return HeatmapColumn(ts_ns, False, 0, array("f"), None, None, trades, liqs)

        base, values = self._book_values(self._bucket_ticks, self.half_range, mean=False)
        return HeatmapColumn(
            ts_ns, True, base, values, bb.price_ticks, ba.price_ticks, trades, liqs
        )

    def _book_values(self, size_ticks: int, half: int, *, mean: bool) -> tuple[int, array[float]]:
        """Liquidität je Bucket der Größe ``size_ticks`` im bekannten Buch, höchstens
        ``half`` Buckets je Seite um den Mid. ``mean``: je Tick-Bucket der Feinauflösung
        mitteln (Dichte), damit grobe und feine Werte dieselbe Farbskala vertragen."""
        bb, ba = self.book.best_bid(), self.book.best_ask()
        assert bb is not None
        assert ba is not None
        center = ((bb.price_ticks + ba.price_ticks) // 2) // size_ticks
        lowest, highest = self.book.extent() or (bb.price_ticks, ba.price_ticks)
        base = max(center - half, lowest // size_ticks)
        end = min(center + half, highest // size_ticks + 1)  # exklusiv
        values = array("f", bytes(4 * (end - base)))
        lo_ticks, hi_ticks = base * size_ticks, end * size_ticks - 1
        for side in (Side.BID, Side.ASK):
            for lvl in self.book.range(side, lo_ticks, hi_ticks):
                values[lvl.price_ticks // size_ticks - base] += float(lvl.qty)
        if mean and self._bucket_ticks:
            k = size_ticks / self._bucket_ticks
            for i, v in enumerate(values):
                if v:
                    values[i] = v / k
        return base, values

    def make_coarse_column(self, ts_ns: int, factor: int, half: int) -> HeatmapColumn:
        """Momentaufnahme für die Langzeit-Heatmap: Buckets = ``factor`` feine Buckets,
        bis ``half`` grobe Buckets je Seite (deutlich breiter als die feine Spalte)."""
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if not self.synced or bb is None or ba is None or self._bucket_ticks is None:
            return HeatmapColumn(ts_ns, False, 0, array("f"), None, None)
        base, values = self._book_values(self._bucket_ticks * factor, half, mean=True)
        return HeatmapColumn(ts_ns, True, base, values, bb.price_ticks, ba.price_ticks)


class LongTermAccumulator:
    """Mittelt feine Heatmap-Spalten zu groben Langzeit-Spalten.

    - Zeit: alle Spalten eines Langzeit-Intervalls (z. B. 50 x 100 ms = 5 s)
    - Preis: je ``factor`` Buckets zu einem

    Mittelwert statt Maximum: über Sekunden zählt, wie *dauerhaft* Liquidität dort lag.
    Ein Maximum ließe jede kurz aufblitzende Order wie eine echte Wall aussehen.

    Feine Spalten sind unterschiedlich lang (bekannter Buchbereich). Der Puffer ist daher
    um die erste Spalte des Intervalls angelegt und auf jeder Seite um ``pad`` Buckets
    breiter; was trotzdem herausfällt (Preis springt im Intervall um mehr als ``pad``),
    wird ignoriert. ``take`` schneidet leere Ränder ab.
    """

    def __init__(self, factor: int, pad: int = 0) -> None:
        if factor < 1:
            raise ValueError("factor muss >= 1 sein")
        self.factor = factor
        self.pad = max(pad, 0)
        self._sum: list[float] | None = None
        self._base = 0  # grober Bucket von _sum[0]
        self._count = 0
        self._bb: int | None = None
        self._ba: int | None = None

    def add(self, col: HeatmapColumn) -> None:
        if not col.synced or not col.values:
            return
        f = self.factor
        if self._sum is None:
            lo = (col.base_bucket - self.pad) // f
            hi = (col.base_bucket + len(col.values) + self.pad) // f + 1
            self._base = lo
            self._sum = [0.0] * (hi - lo + 1)
        s = self._sum
        n = len(s)
        start = col.base_bucket
        base = self._base
        for i, v in enumerate(col.values):
            if v:
                idx = (start + i) // f - base
                if 0 <= idx < n:
                    s[idx] += v
        self._count += 1
        self._bb, self._ba = col.best_bid, col.best_ask

    def take(self, ts_ns: int) -> HeatmapColumn:
        """Liefert die grobe Spalte des abgelaufenen Intervalls und setzt zurück."""
        s = self._sum
        if s is None or self._count == 0:
            col = HeatmapColumn(ts_ns, False, 0, array("f"), None, None)
        else:
            first = next((i for i, v in enumerate(s) if v), 0)
            last = next((i for i in range(len(s) - 1, -1, -1) if s[i]), first - 1)
            div = self._count * self.factor  # Mittel über Zeit und zusammengefasste Buckets
            col = HeatmapColumn(
                ts_ns,
                True,
                self._base + first,
                array("f", (v / div for v in s[first : last + 1])),
                self._bb,
                self._ba,
            )
        self._sum = None
        self._count = 0
        return col
