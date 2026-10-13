"""Konfiguration aus ``config.yaml`` mit Schema-Validierung (pydantic).

Unbekannte Schlüssel sind ein Fehler (``extra="forbid"``): ein Tippfehler in der
YAML soll laut scheitern, statt still auf den Default zurückzufallen.
Secrets gehören NICHT hierher, sondern in Umgebungsvariablen/Keyring.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LoggingConfig(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["console", "json"] = "console"


class SymbolConfig(_Model):
    symbol: Annotated[str, Field(min_length=1)]


class ReconnectConfig(_Model):
    initial_backoff_s: Annotated[float, Field(gt=0)] = 1.0
    max_backoff_s: Annotated[float, Field(gt=0)] = 60.0
    # Keine Nachricht in diesem Zeitraum -> Verbindung gilt als tot, Reconnect.
    stale_timeout_s: Annotated[float, Field(gt=0)] = 30.0
    # Nach so langer stabiler Verbindung wird der Backoff zurückgesetzt.
    stable_after_s: Annotated[float, Field(gt=0)] = 60.0


type DepthLimit = Literal[5, 10, 20, 50, 100, 500, 1000]


class BookValidationConfig(_Model):
    enabled: bool = True
    interval_s: Annotated[float, Field(gt=0)] = 300.0
    snapshot_limit: DepthLimit = 1000
    # Wie viele angewendete Deltas für den Abgleich vorgehalten werden. Muss die
    # REST-Latenz abdecken (100 ms Stream -> 600 = 60 s).
    history_updates: Annotated[int, Field(gt=0)] = 600
    resync_on_mismatch: bool = True


class BinanceUsdmConfig(_Model):
    """Binance USDⓈ-M Futures. Weitere Börsen kommen als eigene Modelle dazu
    (diskriminiert über ``name``)."""

    name: Literal["binance_usdm"]
    enabled: bool = True
    symbols: list[SymbolConfig] = Field(default_factory=list)
    rest_url: str = "https://fapi.binance.com"
    ws_public_url: str = "wss://fstream.binance.com/public"
    ws_market_url: str = "wss://fstream.binance.com/market"
    depth_update_ms: Literal[100, 250, 500] = 100
    snapshot_limit: DepthLimit = 1000
    max_buffered_updates: Annotated[int, Field(gt=0)] = 10_000
    # Refresh, wenn der Mid den inneren Anteil des Snapshot-Preisbereichs verlässt
    # (außerhalb kennt das lokale Buch unveränderte Level nicht).
    coverage_refresh_fraction: Annotated[float, Field(gt=0, le=1)] = 0.5
    validation: BookValidationConfig = Field(default_factory=BookValidationConfig)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)


type ExchangeConfig = BinanceUsdmConfig


class AggregationConfig(_Model):
    interval_ms: Annotated[int, Field(gt=0)] = 100
    bucket_ticks: Annotated[int, Field(gt=0)] = 1


class ServerConfig(_Model):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(gt=0, lt=65536)] = 8765

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # Die Bridge ist lokal gedacht; kein versehentliches Lauschen auf 0.0.0.0.
        if v != "localhost" and not ipaddress.ip_address(v).is_loopback:
            raise ValueError("server.host muss eine Loopback-Adresse sein (z. B. 127.0.0.1)")
        return v


class RecorderConfig(_Model):
    data_dir: Path = Path("data")
    flush_interval_s: Annotated[float, Field(gt=0)] = 5.0
    zstd_level: Annotated[int, Field(ge=1, le=22)] = 9


class AppConfig(_Model):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    exchanges: list[ExchangeConfig] = Field(default_factory=list)
    aggregation: AggregationConfig = Field(default_factory=AggregationConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    recorder: RecorderConfig = Field(default_factory=RecorderConfig)


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)
