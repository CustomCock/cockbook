"""Börsenunabhängiges L2-Orderbuch mit int-Tick-Preisen.

Datenstruktur
-------------
Pro Seite ein ``SortedDict[int, Decimal]`` (sortedcontainers): Preis-Tick -> Menge.

Warum nicht die naheliegenden Alternativen?

- *Normales dict*: Updates O(1), aber Best Bid/Ask und Range-Abfragen bräuchten bei
  jedem Aufruf ein Sortieren aller Level – die Heatmap fragt das ~10x/s ab.
- *Heap*: Best-Preis O(1), aber Löschen beliebiger Level und Range-Abfragen sind
  hässlich (Lazy Deletion) und Ranges gar nicht effizient möglich.
- *Flaches Array, Index = Tick-Offset* (wie ein Ringpuffer): alles O(1), aber der
  Preisbereich muss vorab feststehen und bei Kursbewegung umkopiert werden; tiefe
  Level weit weg vom Mid fressen Speicher. Lohnt sich später evtl. für die
  Aggregation, nicht für das kanonische Buch.

``SortedDict`` hält die Keys in einer Liste sortierter Teil-Listen: Update O(log n),
Best Bid/Ask O(1) via ``peekitem``, Range-Abfrage O(log n + k) via ``irange``. Bei
den paar tausend Leveln eines Krypto-Buchs ist das in reinem Python sehr schnell.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from itertools import islice

from sortedcontainers import SortedDict

from cockbook.core.events import BookDelta, BookSnapshot, PriceLevel, Side

_ZERO = Decimal(0)


class OrderBook:
    def __init__(self) -> None:
        self._bids: SortedDict[int, Decimal] = SortedDict()
        self._asks: SortedDict[int, Decimal] = SortedDict()
        self.last_seq: int | None = None

    # --- Mutationen ---------------------------------------------------------------------

    def clear(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.last_seq = None

    def load(self, bids: Iterable[PriceLevel], asks: Iterable[PriceLevel]) -> None:
        """Ersetzt das komplette Buch (Snapshot)."""
        self.clear()
        self._apply_side(self._bids, bids)
        self._apply_side(self._asks, asks)

    def apply(self, bids: Iterable[PriceLevel], asks: Iterable[PriceLevel]) -> None:
        """Wendet absolute Level-Mengen an; ``qty == 0`` entfernt das Level.

        Ein Löschen für ein nicht vorhandenes Level ist kein Fehler (kommt bei Binance
        laut Doku regulär vor)."""
        self._apply_side(self._bids, bids)
        self._apply_side(self._asks, asks)

    def apply_event(self, event: BookSnapshot | BookDelta) -> None:
        """Bequemlichkeit für Downstream-Consumer, die das Buch aus Bus-Events aufbauen.

        Die Sequenzprüfung ist Aufgabe des Adapters; der Bus liefert nur saubere Folgen
        (Snapshot, dann lückenlose Deltas)."""
        if isinstance(event, BookSnapshot):
            self.load(event.bids, event.asks)
        else:
            self.apply(event.bids, event.asks)
        self.last_seq = event.last_seq

    @staticmethod
    def _apply_side(side: SortedDict[int, Decimal], levels: Iterable[PriceLevel]) -> None:
        for lvl in levels:
            if lvl.qty == _ZERO:
                side.pop(lvl.price_ticks, None)
            else:
                side[lvl.price_ticks] = lvl.qty

    # --- Abfragen -----------------------------------------------------------------------

    def side(self, side: Side) -> Mapping[int, Decimal]:
        """Read-only-Sicht auf eine Seite (sortiert nach Preis aufsteigend)."""
        return self._bids if side is Side.BID else self._asks

    def iter_best(self, side: Side) -> Iterator[tuple[int, Decimal]]:
        """Level einer Seite, beste zuerst (Bid absteigend, Ask aufsteigend)."""
        book = self._bids if side is Side.BID else self._asks
        keys = reversed(book.keys()) if side is Side.BID else iter(book.keys())
        for p in keys:
            yield p, book[p]

    def best_bid(self) -> PriceLevel | None:
        if not self._bids:
            return None
        p, q = self._bids.peekitem(-1)
        return PriceLevel(p, q)

    def best_ask(self) -> PriceLevel | None:
        if not self._asks:
            return None
        p, q = self._asks.peekitem(0)
        return PriceLevel(p, q)

    def qty(self, side: Side, price_ticks: int) -> Decimal:
        return self.side(side).get(price_ticks, _ZERO)

    def top(self, n: int) -> tuple[list[PriceLevel], list[PriceLevel]]:
        """Die besten ``n`` Level je Seite, jeweils vom besten Preis aus gesehen."""
        bids = [PriceLevel(p, self._bids[p]) for p in islice(self._bids.irange(reverse=True), n)]
        asks = [PriceLevel(p, self._asks[p]) for p in islice(self._asks.irange(), n)]
        return bids, asks

    def range(self, side: Side, lo: int, hi: int) -> list[PriceLevel]:
        """Alle Level mit ``lo <= Preis <= hi``, aufsteigend sortiert."""
        book = self._bids if side is Side.BID else self._asks
        return [PriceLevel(p, book[p]) for p in book.irange(lo, hi)]

    def extent(self) -> tuple[int, int] | None:
        """(niedrigster Bid, höchster Ask) in Ticks = bekannter Preisbereich des Buchs."""
        if not self._bids or not self._asks:
            return None
        return self._bids.peekitem(0)[0], self._asks.peekitem(-1)[0]

    def is_crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb.price_ticks >= ba.price_ticks

    @property
    def depth(self) -> tuple[int, int]:
        """Anzahl Level (bids, asks)."""
        return len(self._bids), len(self._asks)
