from decimal import Decimal
from typing import Any

import pytest

from cockbook.core.aggregation import HeatmapAggregator
from cockbook.core.events import BookDelta, BookSnapshot, BookSyncStatus, PriceLevel, Side, Trade

C: dict[str, Any] = {"exchange": "x", "symbol": "S", "ts_recv_ns": 0}


def lv(*pairs: tuple[int, str]) -> tuple[PriceLevel, ...]:
    return tuple(PriceLevel(p, Decimal(q)) for p, q in pairs)


def synced_agg(bucket_ticks: int = 5, half: int = 4) -> HeatmapAggregator:
    a = HeatmapAggregator(bucket_ticks=bucket_ticks, half_range_buckets=half)
    # Bids 95..99, Asks 100..104; Mid = 99.5 -> Ticks 99 -> Bucket 19
    a.on_event(
        BookSnapshot(
            **C,
            bids=lv((99, "1"), (98, "2"), (95, "4"), (80, "100"), (60, "7")),
            asks=lv((100, "1.5"), (104, "3"), (125, "9")),
            last_seq=1,
        )
    )
    a.on_event(BookSyncStatus(**C, synced=True, reason=""))
    return a


def test_buckets_sum_levels_and_window_is_centered() -> None:
    col = synced_agg().make_column(123)
    assert col.synced
    assert col.base_bucket == 19 - 4  # center 19, half 4 -> Buckets 15..22
    assert len(col.values) == 8
    by_bucket = {col.base_bucket + i: v for i, v in enumerate(col.values) if v}
    # Bucket 19 = Ticks 95..99 -> 1 + 2 + 4; Bucket 20 = 100..104 -> 1.5 + 3
    assert by_bucket == {16: 100.0, 19: 7.0, 20: 4.5}
    # 60 (Bucket 12) und 125 (Bucket 25) liegen außerhalb des Fensters


def test_bbo_in_column() -> None:
    col = synced_agg().make_column(0)
    assert (col.best_bid, col.best_ask) == (99, 100)


def test_column_follows_book_updates() -> None:
    a = synced_agg()
    a.on_event(BookDelta(**C, bids=lv((99, "0")), asks=lv((104, "0")), first_seq=2, last_seq=2))
    col = a.make_column(0)
    assert col.best_bid == 98
    assert {col.base_bucket + i: v for i, v in enumerate(col.values) if v}[20] == 1.5


def test_not_synced_gives_empty_column() -> None:
    a = synced_agg()
    a.on_event(BookSyncStatus(**C, synced=False, reason="gap"))
    col = a.make_column(0)
    assert not col.synced
    assert len(col.values) == 0
    assert col.best_bid is None


def test_trades_grouped_by_side_and_bucket_with_exact_vwap() -> None:
    a = synced_agg()
    for price, qty, side in [(100, "1", Side.BID), (101, "3", Side.BID), (99, "2", Side.ASK)]:
        a.on_event(Trade(**C, price_ticks=price, qty=Decimal(qty), aggressor=side))
    col = a.make_column(0)
    bins = {(b.side, round(b.vwap_ticks, 6), b.qty, b.count) for b in col.trades}
    assert bins == {(Side.BID, 100.75, 4.0, 2), (Side.ASK, 99.0, 2.0, 1)}
    assert a.make_column(1).trades == []  # pro Spalte zurückgesetzt


def test_trades_kept_even_when_not_synced() -> None:
    a = HeatmapAggregator(bucket_ticks=1, half_range_buckets=10)
    a.on_event(Trade(**C, price_ticks=5, qty=Decimal(1), aggressor=Side.ASK))
    assert len(a.make_column(0).trades) == 1


def test_invalid_params() -> None:
    with pytest.raises(ValueError, match="> 0"):
        HeatmapAggregator(bucket_ticks=0, half_range_buckets=1)


def test_column_covers_known_book_not_a_window_around_mid() -> None:
    """Liquidität bleibt an ihrem Preis: bewegt sich nur der Mid, ändert sich nichts an
    den Werten weit entfernter Level (früher lag dort der mitlaufende Fensterrand)."""
    a = synced_agg(bucket_ticks=5, half=100)
    col = a.make_column(0)
    assert col.base_bucket == 12  # niedrigster Bid 60 -> Bucket 12
    assert len(col.values) == 25 - 12 + 1  # bis höchster Ask 125 -> Bucket 25
    before = {col.base_bucket + i: v for i, v in enumerate(col.values) if v}
    # Mid springt nach oben (Asks weggekauft), die Wall bei 80 und die Level bei 60 bleiben
    a.on_event(BookDelta(**C, bids=lv((110, "1")), asks=lv((100, "0"), (104, "0"), (115, "2")),
                         first_seq=2, last_seq=2))  # fmt: skip
    after_col = a.make_column(1)
    after = {after_col.base_bucket + i: v for i, v in enumerate(after_col.values) if v}
    assert after[16] == before[16] == 100.0
    assert after[12] == before[12] == 7.0


def test_column_capped_at_half_range() -> None:
    col = synced_agg(bucket_ticks=5, half=2).make_column(0)
    assert col.base_bucket == 17  # center 19 - 2
    assert len(col.values) == 4  # 17..20 (Ask-Seite endet bei Bucket 25, gekappt bei 21)
