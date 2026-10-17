"""Schlüssel für Coin + Börse ("Venue").

Binance bleibt ohne Präfix ("BTCUSDT") – so bleiben gespeicherte Tabs, Einstellungen und
Langzeit-Archive gültig. Andere Börsen bekommen ein Präfix ("BITUNIX:BTCUSDT"), die
kombinierte Ansicht "MULTI:BTCUSDT". Ereignisse selbst tragen weiter das reine Symbol plus
``exchange``; den Schlüssel bildet erst der Feed – Adapter bleiben börsenrein.
"""

from __future__ import annotations

DEFAULT_EXCHANGE = "binance_usdm"
COMBINED = "MULTI"
_PREFIX = {"bitunix": "BITUNIX"}
_EXCHANGE = {v: k for k, v in _PREFIX.items()}


def venue_key(exchange: str, symbol: str) -> str:
    if exchange == DEFAULT_EXCHANGE:
        return symbol
    return f"{_PREFIX.get(exchange, exchange.upper())}:{symbol}"


def split_key(key: str) -> tuple[str, str]:
    """-> (Börse | "MULTI", Symbol)."""
    if ":" not in key:
        return DEFAULT_EXCHANGE, key
    prefix, sym = key.split(":", 1)
    if prefix == COMBINED:
        return COMBINED, sym
    return _EXCHANGE.get(prefix, prefix.lower()), sym


def combined_key(symbol: str) -> str:
    return f"{COMBINED}:{symbol}"
