"""Kombiniertes Buch mehrerer Börsen ("Multibook") für ein Coin.

Jede Börse hat ihre eigene Tick- und Bucket-Größe. Gemeinsame Einheit:

- *Tick* = größter gemeinsamer Teiler aller Tick-Größen (0.1 und 0.01 -> 0.01). Dann ist
  jeder Preis jeder Börse eine exakte ganze Zahl gemeinsamer Ticks – kein Runden, kein
  float als Preis-Key.
- *Bucket* = der gröbste Bucket der Börsen. Mit dem feineren entstünden leere Streifen,
  weil die grobe Börse nur jeden n-ten Bucket füllen könnte.

Eine kombinierte Spalte entsteht aus den fertigen (noch nicht delta-kodierten) Spalten
der einzelnen Börsen im selben Intervall – das kombinierte Buch braucht so keinen eigenen
Zustand und sieht exakt dasselbe wie die Einzelansichten. Synchron ist sie nur, wenn
*alle* Börsen synchron sind: Fehlte eine still, sähe die Liquidität plötzlich halbiert aus.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from decimal import Decimal

from cockbook.core.aggregation import HeatmapColumn, TradeBin
from cockbook.core.events import Side


def decimal_gcd(a: Decimal, b: Decimal) -> Decimal:
    """ggT zweier positiver Dezimalzahlen (z. B. Tick-Größen)."""
    if a <= 0 or b <= 0:
        raise ValueError("Tick-Größen müssen > 0 sein")
    exp = max(0, -int(a.as_tuple().exponent), -int(b.as_tuple().exponent))
    scale = Decimal(10) ** exp
    g = math.gcd(int(a * scale), int(b * scale))
    return (Decimal(g) / scale).normalize()


@dataclass(frozen=True, slots=True)
class Leg:
    """Eine Börse im kombinierten Buch: ``ratio`` = eigene Ticks -> gemeinsame Ticks."""

    tick_size: Decimal
    bucket_ticks: int
    ratio: int


class CombinedBook:
    def __init__(self, legs: list[tuple[Decimal, int]], half_range_buckets: int) -> None:
        """``legs``: je Börse (Tick-Größe, Bucket-Größe in deren Ticks)."""
        if len(legs) < 2:
            raise ValueError("ein kombiniertes Buch braucht mindestens zwei Börsen")
        tick = legs[0][0]
        for t, _ in legs[1:]:
            tick = decimal_gcd(tick, t)
        self.tick_size = tick
        self.legs = [Leg(t, bt, int(t / tick)) for t, bt in legs]
        self.bucket_ticks = max(leg.bucket_ticks * leg.ratio for leg in self.legs)
        self.half_range = half_range_buckets

    def combine(self, ts_ns: int, cols: list[HeatmapColumn]) -> HeatmapColumn:
        """``cols`` in derselben Reihenfolge wie ``legs``."""
        if len(cols) != len(self.legs):
            raise ValueError("eine Spalte je Börse erwartet")
        bt = self.bucket_ticks
        trades: dict[tuple[Side, int], TradeBin] = {}
        liqs: list[tuple[Side, int, float]] = []
        for leg, c in zip(self.legs, cols, strict=True):
            for t in c.trades:
                vwap = t.vwap_ticks * leg.ratio
                key = (t.side, int(vwap // bt))
                acc = trades.get(key)
                if acc is None:
                    acc = trades[key] = TradeBin(t.side)
                acc.qty += t.qty
                acc.notional_ticks += vwap * t.qty
                acc.count += t.count
            liqs.extend((side, p * leg.ratio, q) for side, p, q in c.liquidations)
        bids = [c.best_bid * leg.ratio for leg, c in zip(self.legs, cols, strict=True)
                if c.best_bid is not None]  # fmt: skip
        asks = [c.best_ask * leg.ratio for leg, c in zip(self.legs, cols, strict=True)
                if c.best_ask is not None]  # fmt: skip
        if not all(c.synced for c in cols) or len(bids) < len(cols) or len(asks) < len(cols):
            return HeatmapColumn(
                ts_ns, False, 0, array("f"), None, None, list(trades.values()), liqs
            )
        bb, ba = max(bids), min(asks)
        # Über Börsen hinweg kann das Buch "gekreuzt" sein (Bid hier > Ask dort) – der Mid
        # bleibt trotzdem sinnvoll als Mitte der Ansicht
        center = ((bb + ba) // 2) // bt
        lo, hi = center - self.half_range, center + self.half_range - 1
        sums: dict[int, float] = {}
        for leg, c in zip(self.legs, cols, strict=True):
            f = leg.bucket_ticks * leg.ratio
            for b, v in c.items():
                cb = (b * f) // bt
                if lo <= cb <= hi:
                    sums[cb] = sums.get(cb, 0.0) + v
        keys = sorted(sums)
        base = keys[0] if keys else center
        return HeatmapColumn(
            ts_ns,
            True,
            base,
            array("f", (sums[b] for b in keys)),
            bb,
            ba,
            list(trades.values()),
            liqs,
            array("i", (b - base for b in keys)),
            center,
        )
