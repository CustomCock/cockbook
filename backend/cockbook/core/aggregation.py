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

from sortedcontainers import SortedDict

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

_ZERO = Decimal(0)


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
    # float32. Dicht: Index = bucket - base_bucket. Dünn (``idx`` gesetzt): nur belegte
    # Buckets, ``idx[i]`` = bucket - base_bucket von ``values[i]``.
    values: array[float]
    best_bid: int | None
    best_ask: int | None
    trades: list[TradeBin] = field(default_factory=list)
    # (Seite der Liquidations-Order, Preis in Ticks, Menge) – ASK = Long liquidiert
    liquidations: list[tuple[Side, int, float]] = field(default_factory=list)
    idx: array[int] | None = None
    # Bucket des Mids – das Frontend richtet seinen Puffer daran aus, nicht an der
    # (oft viel breiteren) Spalte
    center: int | None = None
    # Delta-Kodierung (``ColumnDeltaEncoder``): Schlüsselbild = vollständige Spalte,
    # sonst nur geänderte Buckets (0 = Bucket leer). ``seq`` zählt je Symbol lückenlos.
    keyframe: bool = True
    seq: int = 0

    def items(self) -> list[tuple[int, float]]:
        """(bucket, wert) aller belegten Buckets – unabhängig von dicht/dünn."""
        if self.idx is not None:
            return [(self.base_bucket + i, v) for i, v in zip(self.idx, self.values, strict=True)]
        return [(self.base_bucket + i, v) for i, v in enumerate(self.values) if v]


class HeatmapAggregator:
    """``bucket_ticks=None``: Bucket wird beim ersten Snapshot aus ``bucket_bps`` bestimmt
    und bleibt dann fest (eine wechselnde Bucket-Größe würde die Historie unbrauchbar
    machen).

    Bucket-Summen werden bei jedem Delta *inkrementell* nachgeführt (exakt in Decimal –
    float-Summen hätten nach Stunden Rundungsreste, die als Phantom-Liquidität sichtbar
    würden). Eine Spalte liest dann nur noch die Buckets im Fenster; die Kosten hängen
    nicht davon ab, wie viele Level das Buch über die Zeit gelernt hat.

    *Gedächtnis über Neu-Syncs*: Binance liefert per REST nur 1000 Level je Seite (bei BTC
    ±0,2–0,4 % um den Kurs). Alles weiter weg kennt das Buch nur, weil es dort seit dem
    Sync Änderungen gesehen hat. Ein Neu-Sync würde dieses Wissen verwerfen – die Heatmap
    schrumpfte auf ein Band um den Kurs, das mit dem Preis mitwandert. Deshalb bleiben
    Level außerhalb des neuen Snapshots als "zuletzt bekannt" stehen, bis ein Delta sie
    bestätigt/ändert oder sie ``memory_max_age_s`` alt sind.
    """

    def __init__(
        self,
        *,
        bucket_ticks: int | None,
        half_range_buckets: int,
        bucket_bps: float = 0.06,
        memory_max_age_s: float = 3600.0,
    ) -> None:
        if (bucket_ticks is not None and bucket_ticks <= 0) or half_range_buckets <= 0:
            raise ValueError("bucket_ticks und half_range_buckets müssen > 0 sein")
        self._bucket_ticks = bucket_ticks
        self.bucket_bps = bucket_bps
        self.half_range = half_range_buckets
        self.memory_max_age_ns = int(memory_max_age_s * 1e9)
        self.book = OrderBook()
        self.synced = False
        self.instrument: InstrumentInfo | None = None
        self._trades: dict[tuple[Side, int], TradeBin] = {}
        self._liqs: list[tuple[Side, int, float]] = []
        # Bucket -> Summe der Mengen (live Buch bzw. Gedächtnis)
        self._buckets: SortedDict[int, Decimal] = SortedDict()
        self._mem: dict[int, tuple[Decimal, int]] = {}  # Preis -> (Menge, Zeit ns)
        self._mem_buckets: SortedDict[int, Decimal] = SortedDict()
        self._last_prune_ns = 0

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

    # --- Bucket-Summen ------------------------------------------------------------------

    @staticmethod
    def _add(target: SortedDict[int, Decimal], b: int, dq: Decimal) -> None:
        v = target.get(b, _ZERO) + dq
        if v == _ZERO:
            target.pop(b, None)
        else:
            target[b] = v

    def _rebuild_buckets(self) -> None:
        self._buckets.clear()
        if self._bucket_ticks is None:
            return
        bt = self._bucket_ticks
        for side in (Side.BID, Side.ASK):
            for p, q in self.book.side(side).items():
                self._add(self._buckets, p // bt, q)
        self._mem_buckets.clear()
        for p, (q, _) in self._mem.items():
            self._add(self._mem_buckets, p // bt, q)

    def _forget(self, price: int) -> None:
        """Gedächtnis-Level entfernen (jetzt live bekannt oder zu alt)."""
        old = self._mem.pop(price, None)
        if old is not None and self._bucket_ticks is not None:
            self._add(self._mem_buckets, price // self._bucket_ticks, -old[0])

    def _remember_book(self, ts_ns: int) -> None:
        for side in (Side.BID, Side.ASK):
            for p, q in self.book.side(side).items():
                self._forget(p)
                self._mem[p] = (q, ts_ns)
                if self._bucket_ticks is not None:
                    self._add(self._mem_buckets, p // self._bucket_ticks, q)

    def _prune_memory(self, now_ns: int) -> None:
        if now_ns - self._last_prune_ns < 10_000_000_000 or not self._mem:
            return
        self._last_prune_ns = now_ns
        limit = now_ns - self.memory_max_age_ns
        for p in [p for p, (_, ts) in self._mem.items() if ts < limit]:
            self._forget(p)

    @property
    def remembered_levels(self) -> int:
        return len(self._mem)

    # --- Eingang ------------------------------------------------------------------------

    def on_event(self, e: Event) -> None:
        if isinstance(e, BookSnapshot):
            self.book.apply_event(e)
            if self._bucket_ticks is None and self.instrument is not None:
                bb, ba = self.book.best_bid(), self.book.best_ask()
                if bb is not None and ba is not None:
                    mid = (bb.price_ticks + ba.price_ticks) // 2
                    self._bucket_ticks = auto_bucket_ticks(
                        mid, self.instrument.tick_size, self.bucket_bps
                    )
            # Gedächtnis im Bereich des Snapshots ist jetzt durch echte Daten ersetzt
            ext = self.book.extent()
            if ext is not None and self._mem:
                lo, hi = ext
                for p in [p for p in self._mem if lo <= p <= hi]:
                    self._mem.pop(p)
            self._rebuild_buckets()
        elif isinstance(e, BookDelta):
            bt = self._bucket_ticks
            if bt is not None:
                for side, levels in ((Side.BID, e.bids), (Side.ASK, e.asks)):
                    for lvl in levels:
                        old = self.book.qty(side, lvl.price_ticks)
                        if lvl.qty != old:
                            self._add(self._buckets, lvl.price_ticks // bt, lvl.qty - old)
                        if self._mem:
                            self._forget(lvl.price_ticks)
            self.book.apply_event(e)
        elif isinstance(e, BookSyncStatus):
            self.synced = e.synced
            if not e.synced:
                self._remember_book(e.ts_ns)
                self.book.clear()
                self._buckets.clear()
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

    def _window(self, lo_b: int, hi_b: int) -> dict[int, Decimal]:
        """Bucket-Summen (live + Gedächtnis) mit ``lo_b <= bucket <= hi_b``."""
        out: dict[int, Decimal] = {b: self._buckets[b] for b in self._buckets.irange(lo_b, hi_b)}
        for b in self._mem_buckets.irange(lo_b, hi_b):
            out[b] = out.get(b, _ZERO) + self._mem_buckets[b]
        return out

    def make_column(self, ts_ns: int) -> HeatmapColumn:
        trades = list(self._trades.values())
        self._trades = {}
        liqs, self._liqs = self._liqs, []
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if not self.synced or bb is None or ba is None or self._bucket_ticks is None:
            return HeatmapColumn(ts_ns, False, 0, array("f"), None, None, trades, liqs)
        self._prune_memory(ts_ns)
        center = self.bucket((bb.price_ticks + ba.price_ticks) // 2)
        win = self._window(center - self.half_range, center + self.half_range - 1)
        keys = sorted(b for b, v in win.items() if v > 0)
        if not keys:
            return HeatmapColumn(
                ts_ns, True, center, array("f"), bb.price_ticks, ba.price_ticks, trades, liqs,
                array("i"), center,
            )  # fmt: skip
        base = keys[0]
        return HeatmapColumn(
            ts_ns,
            True,
            base,
            array("f", (float(win[b]) for b in keys)),
            bb.price_ticks,
            ba.price_ticks,
            trades,
            liqs,
            array("i", (b - base for b in keys)),
            center,
        )

    def coarse_sums(self, factor: int, half: int) -> dict[int, float] | None:
        """Momentaufnahme für die Langzeit-Heatmap: Summe je grobem Bucket (= ``factor``
        feine Buckets), höchstens ``half`` grobe Buckets je Seite um den Mid.
        None = Buch nicht synchron."""
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if not self.synced or bb is None or ba is None or self._bucket_ticks is None:
            return None
        center = self.bucket((bb.price_ticks + ba.price_ticks) // 2) // factor
        win = self._window((center - half) * factor, (center + half + 1) * factor - 1)
        out: dict[int, float] = {}
        for b, v in win.items():
            if v > 0:
                cb = b // factor
                out[cb] = out.get(cb, 0.0) + float(v)
        return out


class ColumnDeltaEncoder:
    """Macht aus vollständigen Spalten Schlüsselbilder + Änderungen (wie Videokompression).

    Bitunix liefert das ganze Buch (BTC: ~6000 belegte Buckets ≈ 50 KB je 100-ms-Spalte),
    geändert sind davon pro Spalte meist nur ein paar Dutzend. Statt dafür im Aggregator
    jede Bucket-Änderung mitzuschreiben, vergleicht der Encoder mit der zuletzt *gesendeten*
    Spalte: Das erfasst automatisch auch Buckets, die durch eine Verschiebung des Fensters
    oder das Altern des Gedächtnisses wegfallen – genau die Fälle, die man beim Mitschreiben
    leicht vergisst. Kosten: ein Dict-Vergleich je Spalte, O(belegte Buckets).

    Schlüsselbild, wenn: erste Spalte, Spalte nicht synchron, alle ``keyframe_every``
    Spalten (damit neue/abgehängte Clients schnell wieder einsteigen) oder wenn die
    Änderungen mehr als halb so groß wären wie die ganze Spalte (z. B. großer Kurssprung).
    """

    def __init__(self, keyframe_every: int) -> None:
        if keyframe_every < 1:
            raise ValueError("keyframe_every muss >= 1 sein")
        self.keyframe_every = keyframe_every
        self._prev: dict[int, float] | None = None
        self._since_key = 0
        self._seq = 0

    def encode(self, col: HeatmapColumn) -> HeatmapColumn:
        self._seq += 1
        col.seq = self._seq
        if not col.synced:
            self._prev = None
            col.keyframe = True
            return col
        cur = dict(col.items())
        prev = self._prev
        self._prev = cur
        if prev is not None and self._since_key + 1 < self.keyframe_every:
            changed = [(b, v) for b, v in cur.items() if prev.get(b) != v]
            changed.extend((b, 0.0) for b in prev.keys() - cur.keys())
            if 2 * len(changed) <= len(cur):
                self._since_key += 1
                changed.sort()
                base = changed[0][0] if changed else (col.center or 0)
                col.base_bucket = base
                col.idx = array("i", (b - base for b, _ in changed))
                col.values = array("f", (v for _, v in changed))
                col.keyframe = False
                return col
        self._since_key = 0
        col.keyframe = True
        if col.idx is None:  # dichte Spalte -> dünn, damit beide Fälle gleich aussehen
            items = sorted(cur.items())
            base = items[0][0] if items else col.base_bucket
            col.base_bucket = base
            col.idx = array("i", (b - base for b, _ in items))
            col.values = array("f", (v for _, v in items))
        return col


class LongTermAccumulator:
    """Eine Stufe der Langzeit-Heatmap (z. B. 1-Minuten-Spalten für die 1D-Ansicht).

    Innerhalb eines Intervalls wird das Buch mehrmals abgetastet (``add_sample``); die
    Spalte ist der *Mittelwert* je grobem Bucket, ausgedrückt als Dichte je feinem Bucket
    (Summe / Abtastungen / ``factor``) – so vertragen alle Stufen dieselbe Farbskala.
    Mittelwert statt Maximum: über Minuten zählt, wie *dauerhaft* Liquidität dort lag; ein
    Maximum ließe jede kurz aufblitzende Order wie eine Wall aussehen.

    Zusätzlich: Liquidationen (USDT) im Intervall, getrennt nach Longs/Shorts.
    """

    def __init__(self, factor: int) -> None:
        if factor < 1:
            raise ValueError("factor muss >= 1 sein")
        self.factor = factor
        self._sum: dict[int, float] = {}
        self._count = 0
        self._bb: int | None = None
        self._ba: int | None = None
        self.liq_long = 0.0
        self.liq_short = 0.0

    def add_sample(self, sums: dict[int, float] | None, bb: int | None, ba: int | None) -> None:
        if sums is None:
            return
        s = self._sum
        for b, v in sums.items():
            s[b] = s.get(b, 0.0) + v
        self._count += 1
        self._bb, self._ba = bb, ba

    def add_liquidation(self, side: Side, usd: float) -> None:
        # Liquidations-Order SELL (ASK) = Long liquidiert
        if side is Side.ASK:
            self.liq_long += usd
        else:
            self.liq_short += usd

    def take(self, ts_ns: int) -> HeatmapColumn:
        """Liefert die grobe Spalte des abgelaufenen Intervalls (dünn) und setzt zurück."""
        if not self._sum or self._count == 0:
            col = HeatmapColumn(ts_ns, False, 0, array("f"), None, None, idx=array("i"))
        else:
            keys = sorted(self._sum)
            base = keys[0]
            div = self._count * self.factor
            col = HeatmapColumn(
                ts_ns,
                True,
                base,
                array("f", (self._sum[b] / div for b in keys)),
                self._bb,
                self._ba,
                idx=array("i", (b - base for b in keys)),
            )
        self._sum = {}
        self._count = 0
        return col

    def take_liquidations(self) -> tuple[float, float]:
        out = (self.liq_long, self.liq_short)
        self.liq_long = self.liq_short = 0.0
        return out
