from decimal import Decimal

import pytest

from cockbook.core.instrument import Instrument

BTC = Instrument("x", "BTCUSDT", Decimal("0.10"), Decimal("0.001"))


@pytest.mark.parametrize(
    ("price", "ticks"),
    [("65000.10", 650001), ("65000.1", 650001), ("0.10", 1), ("65000", 650000)],
)
def test_price_to_ticks_exact(price: str, ticks: int) -> None:
    assert BTC.price_to_ticks(price) == ticks


def test_price_not_on_grid_raises() -> None:
    with pytest.raises(ValueError, match="Vielfaches"):
        BTC.price_to_ticks("65000.15")


def test_rounded_for_average_prices() -> None:
    assert BTC.price_to_ticks_rounded("64995.37") == 649954
    assert BTC.price_to_ticks_rounded("64995.32") == 649953


def test_roundtrip_keeps_tick_formatting() -> None:
    assert str(BTC.ticks_to_price(650001)) == "65000.10"


def test_no_float_artifacts() -> None:
    # Mit float wäre 0.1 + 0.2 != 0.3 – mit Ticks ist es exakt.
    assert BTC.price_to_ticks("0.1") + BTC.price_to_ticks("0.2") == BTC.price_to_ticks("0.3")


def test_invalid_tick_size() -> None:
    with pytest.raises(ValueError, match="Tick"):
        Instrument("x", "S", Decimal(0), Decimal(1))
