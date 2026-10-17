"""Paper-Trading: Fill-Modell, Positionen, TP/SL, Risiko-Grenzen, Speichern."""

from __future__ import annotations

import json
from collections.abc import Iterable
from decimal import Decimal as D  # noqa: N817 – kurz, viele Zahlen

import pytest

from cockbook.config import PaperConfig
from cockbook.core.events import Side
from cockbook.trading.paper.engine import (
    OrderRejectedError,
    PaperEngine,
    account_from_json,
    account_to_json,
)

NOW = 1_800_000_000_000_000_000


class Book:
    """Festes Test-Buch: Preis -> Menge je Seite."""

    exchange = "binance_usdm"
    tick_size = D("0.1")
    step_size = D("0.001")

    def __init__(self, bids: dict[str, str], asks: dict[str, str]) -> None:
        self.set(bids, asks)

    def set(self, bids: dict[str, str], asks: dict[str, str]) -> None:
        self.bids = {D(p): D(q) for p, q in bids.items()}
        self.asks = {D(p): D(q) for p, q in asks.items()}

    def bbo(self) -> tuple[D, D] | None:
        if not self.bids or not self.asks:
            return None
        return max(self.bids), min(self.asks)

    def level_qty(self, side: Side, price: D) -> D:
        return (self.bids if side is Side.BID else self.asks).get(price, D(0))

    def levels(self, side: Side) -> Iterable[tuple[D, D]]:
        if side is Side.BID:
            return sorted(self.bids.items(), reverse=True)
        return sorted(self.asks.items())


def engine(book: Book, **cfg: object) -> PaperEngine:
    return PaperEngine(PaperConfig(**cfg), lambda s: book if s == "BTC" else None)


def std_book() -> Book:
    return Book(
        {"99.9": "2", "99.8": "5", "99.0": "5"}, {"100.0": "1", "100.1": "3", "101.0": "10"}
    )


def test_market_walks_the_book_with_taker_fee() -> None:
    b = std_book()
    e = engine(b)
    o = e.place("BTC", "buy", "market", D("2.5"), now_ns=NOW)
    assert o.filled == D("2.5")
    fill = e.acc.fills[-1]
    # 1 @ 100.0 + 1.5 @ 100.1 -> VWAP 100.06
    assert fill.price == D("100.06")
    assert fill.liquidity == "taker"
    assert fill.fee == D("2.5") * D("100.06") * D("0.0005")
    pos = e.acc.positions["BTC"]
    assert pos.qty == D("2.5")
    assert pos.entry == D("100.06")
    assert e.acc.wallet == D(10_000) - fill.fee


def test_market_rejected_when_book_too_thin() -> None:
    e = engine(Book({"99": "1"}, {}))
    with pytest.raises(OrderRejectedError, match="nicht synchron"):
        e.place("BTC", "buy", "market", D(1), now_ns=NOW)
    with pytest.raises(OrderRejectedError, match="kein Orderbuch"):
        e.place("ETH", "buy", "market", D(1), now_ns=NOW)


def test_limit_waits_for_queue_ahead() -> None:
    b = std_book()
    e = engine(b)
    o = e.place("BTC", "buy", "limit", D(1), price=D("99.8"), now_ns=NOW)
    assert o.queue_ahead == D(5)  # dort lagen schon 5
    e.on_trade("BTC", D("99.8"), D(3), Side.ASK, NOW)  # Verkäufer arbeiten 3 ab
    assert o.id in e.acc.orders
    assert e.acc.orders[o.id].queue_ahead == D(2)
    e.on_trade("BTC", D("99.8"), D(1), Side.BID, NOW)  # Käufer-Aggressor: trifft uns nicht
    assert e.acc.orders[o.id].queue_ahead == D(2)
    e.on_trade("BTC", D("99.8"), D("2.4"), Side.ASK, NOW)  # 2 Warteschlange, 0.4 für uns
    assert e.acc.orders[o.id].filled == D("0.4")
    e.on_trade("BTC", D("99.8"), D(5), Side.ASK, NOW)
    assert o.id not in e.acc.orders
    assert e.acc.positions["BTC"].qty == D(1)
    assert {f.liquidity for f in e.acc.fills} == {"maker"}


def test_limit_cancels_ahead_shrink_queue_and_trade_through_fills() -> None:
    b = std_book()
    e = engine(b)
    o = e.place("BTC", "buy", "limit", D(1), price=D("99.8"), now_ns=NOW)
    b.set({"99.9": "2", "99.8": "1"}, {"100.0": "1"})  # 4 vor uns gecancelt
    e.on_tick(NOW)
    assert e.acc.orders[o.id].queue_ahead == D(1)
    e.on_trade("BTC", D("99.7"), D("0.01"), Side.ASK, NOW)  # durch unseren Preis
    assert e.acc.positions["BTC"].qty == D(1)
    assert e.acc.fills[-1].price == D("99.8")


def test_limit_filled_when_book_moves_through() -> None:
    b = std_book()
    e = engine(b)
    e.place("BTC", "sell", "limit", D(1), price=D("100.5"), now_ns=NOW)
    e.on_tick(NOW)
    assert not e.acc.positions
    b.set({"100.5": "3"}, {"100.6": "1"})  # Bid jetzt auf unserem Limit
    e.on_tick(NOW)
    assert e.acc.positions["BTC"].qty == D(-1)


def test_marketable_limit_executes_up_to_limit_and_rests() -> None:
    e = engine(std_book())
    o = e.place("BTC", "buy", "limit", D(5), price=D("100.1"), now_ns=NOW)
    assert e.acc.positions["BTC"].qty == D(4)  # 1 @ 100.0 + 3 @ 100.1
    assert e.acc.orders[o.id].remaining == D(1)


def test_stop_triggers_at_mid() -> None:
    b = std_book()
    e = engine(b)
    with pytest.raises(OrderRejectedError, match="falschen Seite"):
        e.place("BTC", "buy", "stop", D(1), trigger=D(99), now_ns=NOW)
    e.place("BTC", "buy", "stop", D(1), trigger=D("100.5"), now_ns=NOW)
    e.on_tick(NOW)
    assert not e.acc.positions
    b.set({"100.5": "5"}, {"100.7": "5"})
    e.on_tick(NOW)
    assert e.acc.positions["BTC"].entry == D("100.7")
    assert not e.acc.orders


def test_bracket_tp_and_sl_close_and_journal() -> None:
    b = std_book()
    e = engine(b)
    with pytest.raises(OrderRejectedError, match="Take-Profit"):
        e.place("BTC", "buy", "market", D(1), tp=D(90), now_ns=NOW)
    e.place("BTC", "buy", "market", D(1), tp=D(105), sl=D(98), now_ns=NOW)
    pos = e.acc.positions["BTC"]
    assert (pos.tp, pos.sl) == (D(105), D(98))
    b.set({"105.0": "5"}, {"105.2": "5"})
    e.on_tick(NOW + 1)
    assert not e.acc.positions
    t = e.acc.trades[-1]
    assert t.reason == "Take-Profit"
    assert t.side == "long"
    assert t.exit == D(105)
    assert t.pnl == D(5) - t.fees
    assert e.messages[-1] == ("ok", "BTC: Take-Profit ausgeführt")


def test_flip_and_reduce_only() -> None:
    e = engine(std_book())
    e.place("BTC", "buy", "market", D(1), now_ns=NOW)
    with pytest.raises(OrderRejectedError, match="Reduce-Only"):
        e.place("BTC", "buy", "limit", D(1), price=D(99), reduce_only=True, now_ns=NOW)
    ro = e.place("BTC", "sell", "limit", D(5), price=D(102), reduce_only=True, now_ns=NOW)
    assert ro.qty == D(1)  # auf Positionsgröße gekürzt
    e.place("BTC", "sell", "market", D(3), now_ns=NOW)  # 1 schließen, 2 short
    pos = e.acc.positions["BTC"]
    assert pos.qty == D(-2)
    # ein Fill zum Durchschnitt (2 @ 99.9 + 1 @ 99.8): 1 schließt, 2 eröffnen den Short
    assert pos.entry == D("99.867")
    assert ro.id not in e.acc.orders  # Reduce-Only fällt mit der alten Position weg


def test_risk_limits() -> None:
    e = engine(std_book(), start_balance=1000, max_position_usd=500)
    with pytest.raises(OrderRejectedError, match="Limit"):
        e.place("BTC", "buy", "market", D(6), now_ns=NOW)
    e2 = engine(std_book(), start_balance=10, default_leverage=10)
    with pytest.raises(OrderRejectedError, match="Margin"):
        e2.place("BTC", "buy", "market", D(2), now_ns=NOW)
    with pytest.raises(OrderRejectedError, match="Hebel"):
        e2.set_leverage("BTC", 500)


def test_daily_loss_limit_blocks_new_positions_but_allows_closing() -> None:
    b = std_book()
    e = engine(b, daily_loss_limit_usd=1)
    e.place("BTC", "buy", "market", D(1), now_ns=NOW)
    b.set({"95.0": "5"}, {"95.1": "5"})
    with pytest.raises(OrderRejectedError, match="Tagesverlust"):
        e.place("BTC", "buy", "market", D("0.1"), now_ns=NOW)
    e.close("BTC", NOW)
    assert not e.acc.positions


def test_liquidation_loses_at_most_the_margin() -> None:
    b = std_book()
    e = engine(b, default_leverage=10)
    e.place("BTC", "buy", "market", D(1), now_ns=NOW)
    pos = e.acc.positions["BTC"]
    margin = pos.margin
    wallet = e.acc.wallet
    b.set({"80.0": "5"}, {"80.1": "5"})  # weit unter dem Liquidationspreis (~90.5)
    e.on_tick(NOW)
    assert not e.acc.positions
    assert e.acc.trades[-1].reason == "Liquidation"
    assert e.acc.wallet == wallet - margin


def test_kill_switch_and_breakeven() -> None:
    e = engine(std_book())
    e.place("BTC", "buy", "market", D(1), now_ns=NOW)
    e.place("BTC", "buy", "limit", D(1), price=D(99), now_ns=NOW)
    snap = e.snapshot()
    be = snap["positions"][0]["breakeven"]
    # Einstieg 100 + Kauf- und Verkaufsgebühr
    assert be == pytest.approx(100 * (1 + 0.0005) / (1 - 0.0005), rel=1e-9)
    assert e.close_all(NOW) == []
    assert not e.acc.positions
    assert not e.acc.orders


def test_persistence_roundtrip() -> None:
    e = engine(std_book())
    e.place("BTC", "buy", "market", D(1), tp=D(110), now_ns=NOW)
    e.place("BTC", "sell", "limit", D("0.5"), price=D(102), now_ns=NOW)
    e.close("BTC", NOW)
    e.place("BTC", "sell", "market", D(1), now_ns=NOW)
    raw = json.loads(json.dumps(account_to_json(e.acc)))
    restored = account_from_json(raw)
    assert restored == e.acc
