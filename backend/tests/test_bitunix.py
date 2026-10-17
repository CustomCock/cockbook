"""Bitunix: volle Bücher -> Snapshot/Deltas, Paare, Trades, Ticker (feste Fixtures)."""

from decimal import Decimal
from typing import Any

import pytest

from cockbook.adapters.bitunix import BitunixBookDiffer, parse_pairs, parse_ticker, parse_trade
from cockbook.core.events import BookDelta, BookSnapshot, BookSyncStatus, Side
from cockbook.core.orderbook import OrderBook

PAIRS: list[dict[str, Any]] = [
    {"symbol": "BTCUSDT", "quotePrecision": 1, "basePrecision": 4, "symbolStatus": "OPEN"},
    {"symbol": "XRPUSDT", "quotePrecision": 4, "basePrecision": 0, "symbolStatus": "OPEN"},
    {"symbol": "OLDUSDT", "quotePrecision": 2, "basePrecision": 2, "symbolStatus": "STOP"},
]


def test_parse_pairs_ticks_from_precision() -> None:
    p = parse_pairs(PAIRS)
    assert set(p) == {"BTCUSDT", "XRPUSDT"}  # STOP fällt raus
    assert p["BTCUSDT"].tick_size == Decimal("0.1")
    assert p["BTCUSDT"].step_size == Decimal("0.0001")
    assert p["XRPUSDT"].step_size == Decimal(1)


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> dict[str, Any]:
    return {"b": [list(x) for x in bids], "a": [list(x) for x in asks]}


def test_differ_snapshot_then_only_changes() -> None:
    inst = parse_pairs(PAIRS)["BTCUSDT"]
    d = BitunixBookDiffer(inst)
    ev = d.on_book(book([("100.0", "1"), ("99.9", "2")], [("100.1", "3")]), 1, 1)
    assert isinstance(ev[0], BookSnapshot)
    assert isinstance(ev[1], BookSyncStatus)
    assert ev[1].synced
    assert [(lv.price_ticks, lv.qty) for lv in ev[0].bids] == [
        (1000, Decimal(1)),
        (999, Decimal(2)),
    ]
    # gleiche Nachricht -> nichts zu tun
    assert d.on_book(book([("100.0", "1"), ("99.9", "2")], [("100.1", "3")]), 2, 2) == []
    # 100.0 geändert, 99.9 weg, 99.8 neu; Ask unverändert
    ev = d.on_book(book([("100.0", "5"), ("99.8", "4")], [("100.1", "3")]), 3, 3)
    assert len(ev) == 1
    delta = ev[0]
    assert isinstance(delta, BookDelta)
    assert {(lv.price_ticks, lv.qty) for lv in delta.bids} == {
        (1000, Decimal(5)),
        (998, Decimal(4)),
        (999, Decimal(0)),
    }
    assert delta.asks == ()
    assert delta.prev_last_seq == delta.last_seq - 1


def test_differ_reconstructs_book_exactly() -> None:
    """Snapshot + alle Deltas angewendet == letztes volles Buch."""
    inst = parse_pairs(PAIRS)["BTCUSDT"]
    d = BitunixBookDiffer(inst)
    ob = OrderBook()
    msgs = [
        book([("100.0", "1"), ("99.9", "2"), ("80.0", "50")], [("100.1", "3"), ("150.0", "9")]),
        book([("100.0", "1.5"), ("80.0", "50")], [("100.1", "3"), ("100.2", "1")]),
        book([("99.9", "7"), ("80.0", "49.5")], [("100.2", "1"), ("150.0", "9")]),
    ]
    for i, m in enumerate(msgs):
        for e in d.on_book(m, i, i):
            if isinstance(e, BookSnapshot | BookDelta):
                ob.apply_event(e)
    last = msgs[-1]
    assert {p: q for p, q in ob.side(Side.BID).items()} == {
        inst.price_to_ticks(p): Decimal(q) for p, q in last["b"]
    }
    assert {p: q for p, q in ob.side(Side.ASK).items()} == {
        inst.price_to_ticks(p): Decimal(q) for p, q in last["a"]
    }


def test_differ_reset_gives_new_snapshot() -> None:
    d = BitunixBookDiffer(parse_pairs(PAIRS)["BTCUSDT"])
    d.on_book(book([("100.0", "1")], [("100.1", "1")]), 1, 1)
    d.reset()
    assert not d.synced
    assert isinstance(d.on_book(book([("100.0", "1")], [("100.1", "1")]), 2, 2)[0], BookSnapshot)


def test_differ_rejects_off_tick_prices() -> None:
    d = BitunixBookDiffer(parse_pairs(PAIRS)["BTCUSDT"])
    with pytest.raises(ValueError, match="Vielfaches"):
        d.on_book(book([("100.05", "1")], [("100.1", "1")]), 1, 1)


def test_trade_and_ticker() -> None:
    inst = parse_pairs(PAIRS)["BTCUSDT"]
    t = parse_trade(
        {"t": "2026-04-07T05:47:52Z", "p": "68621.4", "v": "0.7142", "s": "buy"}, inst, 5, 6
    )
    assert (t.price_ticks, t.qty, t.aggressor, t.ts_exchange_ns) == (
        686214,
        Decimal("0.7142"),
        Side.BID,
        5_000_000,
    )
    assert parse_trade({"p": "1.0", "v": "1", "s": "sell"}, inst, 0, 0).aggressor is Side.ASK
    tk = parse_ticker(
        {"s": "BTCUSDT", "la": "2", "o": "1", "h": "3", "l": "0.5", "b": "10", "q": "20"}, 7, 8
    )
    assert (tk.symbol, tk.last, tk.quote_volume, tk.exchange) == (
        "BTCUSDT",
        Decimal(2),
        Decimal(20),
        "bitunix",
    )
