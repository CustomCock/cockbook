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


class ExchangeConfig(_Model):
    name: Annotated[str, Field(min_length=1)]
    enabled: bool = True
    symbols: list[SymbolConfig] = Field(default_factory=list)


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
