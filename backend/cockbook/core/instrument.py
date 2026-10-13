"""Umrechnung zwischen Börsen-Preisstrings und internen int-Ticks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal


@dataclass(frozen=True, slots=True)
class Instrument:
    exchange: str
    symbol: str
    tick_size: Decimal
    step_size: Decimal

    def __post_init__(self) -> None:
        if self.tick_size <= 0 or self.step_size <= 0:
            raise ValueError(f"ungültige Tick-/Step-Größe für {self.symbol}")

    def price_to_ticks(self, price: str | Decimal) -> int:
        """Exakte Umrechnung. Ein Preis, der kein Vielfaches der Tick-Größe ist, ist ein
        Fehler (falsche Tick-Größe oder kaputte Nachricht) und wird nicht still gerundet."""
        q, r = divmod(Decimal(price), self.tick_size)
        if r != 0:
            raise ValueError(f"Preis {price} ist kein Vielfaches von {self.tick_size}")
        return int(q)

    def price_to_ticks_rounded(self, price: str | Decimal) -> int:
        """Für Werte, die legitim zwischen Ticks liegen (z. B. Durchschnittspreise)."""
        return int((Decimal(price) / self.tick_size).to_integral_value(ROUND_HALF_EVEN))

    def ticks_to_price(self, ticks: int) -> Decimal:
        return ticks * self.tick_size
