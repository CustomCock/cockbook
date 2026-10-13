from decimal import Decimal

from cockbook.core.events import BookDelta, BookSnapshot, PriceLevel, Side
from cockbook.core.orderbook import OrderBook


def lv(p: int, q: str) -> PriceLevel:
    return PriceLevel(p, Decimal(q))


def make_book() -> OrderBook:
    b = OrderBook()
    b.load(
        bids=[lv(100, "1"), lv(99, "2"), lv(97, "3"), lv(95, "0")],
        asks=[lv(101, "1.5"), lv(102, "2"), lv(105, "4")],
    )
    return b


def test_load_skips_zero_levels_and_finds_best() -> None:
    b = make_book()
    assert b.depth == (3, 3)
    assert b.best_bid() == lv(100, "1")
    assert b.best_ask() == lv(101, "1.5")


def test_apply_update_insert_and_delete() -> None:
    b = make_book()
    b.apply(bids=[lv(100, "0"), lv(98, "7")], asks=[lv(101, "3")])
    assert b.best_bid() == lv(99, "2")
    assert b.qty(Side.BID, 98) == Decimal(7)
    assert b.qty(Side.ASK, 101) == Decimal(3)
    assert b.qty(Side.BID, 100) == 0


def test_delete_unknown_level_is_noop() -> None:
    b = make_book()
    b.apply(bids=[lv(50, "0")], asks=[lv(500, "0")])
    assert b.depth == (3, 3)


def test_top_is_ordered_from_best_price() -> None:
    bids, asks = make_book().top(2)
    assert [lvl.price_ticks for lvl in bids] == [100, 99]
    assert [lvl.price_ticks for lvl in asks] == [101, 102]


def test_top_with_more_levels_than_available() -> None:
    bids, asks = make_book().top(50)
    assert len(bids) == 3
    assert len(asks) == 3


def test_range_query_inclusive_ascending() -> None:
    b = make_book()
    assert [lvl.price_ticks for lvl in b.range(Side.BID, 97, 99)] == [97, 99]
    assert [lvl.price_ticks for lvl in b.range(Side.ASK, 102, 200)] == [102, 105]


def test_empty_book() -> None:
    b = OrderBook()
    assert b.best_bid() is None
    assert b.best_ask() is None
    assert not b.is_crossed()


def test_crossed_detection() -> None:
    b = make_book()
    assert not b.is_crossed()
    b.apply(bids=[lv(101, "1")], asks=[])
    assert b.is_crossed()


def test_apply_event_tracks_seq() -> None:
    b = OrderBook()
    b.apply_event(
        BookSnapshot(
            exchange="x",
            symbol="S",
            ts_recv_ns=0,
            bids=(lv(10, "1"),),
            asks=(lv(11, "1"),),
            last_seq=5,
        )
    )
    b.apply_event(
        BookDelta(
            exchange="x",
            symbol="S",
            ts_recv_ns=0,
            bids=(lv(10, "0"),),
            asks=(),
            first_seq=6,
            last_seq=8,
            prev_last_seq=5,
        )
    )
    assert b.last_seq == 8
    assert b.best_bid() is None


def test_snapshot_replaces_everything() -> None:
    b = make_book()
    b.load(bids=[lv(1, "1")], asks=[lv(2, "1")])
    assert b.depth == (1, 1)
