import dataclasses
from decimal import Decimal

import pytest

from cockbook.core.events import (
    BookDelta,
    Event,
    PriceLevel,
    Side,
    Trade,
)


def _trade(**kw: object) -> Trade:
    base: dict[str, object] = {
        "exchange": "test",
        "symbol": "BTCUSDT",
        "ts_recv_ns": 2_000,
        "price_ticks": 650_001,
        "qty": Decimal("0.010"),
        "aggressor": Side.BID,
    }
    base.update(kw)
    return Trade(**base)  # type: ignore[arg-type]


def test_ts_prefers_exchange_time() -> None:
    assert _trade(ts_exchange_ns=1_000).ts_ns == 1_000


def test_ts_falls_back_to_recv_time() -> None:
    assert _trade().ts_ns == 2_000


def test_events_are_immutable() -> None:
    t = _trade()
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.qty = Decimal(1)  # type: ignore[misc]


def test_events_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        Trade("test", "BTCUSDT", 1, None, 1, Decimal(1), Side.BID)  # type: ignore[call-arg]


def test_side_opposite() -> None:
    assert Side.BID.opposite is Side.ASK
    assert Side.ASK.opposite is Side.BID


def test_delta_is_event_subclass_and_hashable() -> None:
    d = BookDelta(
        exchange="test",
        symbol="BTCUSDT",
        ts_recv_ns=1,
        bids=(PriceLevel(100, Decimal("1.5")),),
        asks=(PriceLevel(101, Decimal(0)),),
        first_seq=10,
        last_seq=12,
        prev_last_seq=9,
    )
    assert isinstance(d, Event)
    assert hash(d) == hash(dataclasses.replace(d))
    # Decimal vergleicht exakt, unabhängig von der String-Darstellung der Börse
    assert PriceLevel(1, Decimal("1.50")) == PriceLevel(1, Decimal("1.5"))
