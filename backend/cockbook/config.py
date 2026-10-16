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
    # Optional fest: Preis-Bucket in Ticks. Sonst automatisch aus aggregation.bucket_bps.
    bucket_ticks: Annotated[int, Field(gt=0)] | None = None
    # Orderbuch + Heatmap ab Start. Sonst erst, wenn der Coin im Frontend gewählt wird
    # (Kennzahlen wie Preis/Funding gibt es für alle Coins immer).
    always_on: bool = False
    # Anzeigename, z. B. "SHIB (x1000)"; Standard: Symbol ohne USDT
    label: str | None = None


class MetricsConfig(_Model):
    """Derivate-Kennzahlen per REST (Open Interest, Long/Short, Taker-Volumen)."""

    enabled: bool = True
    open_interest_interval_s: Annotated[float, Field(ge=5)] = 15.0
    # Ratios werden von Binance in Perioden geliefert; öfter abfragen bringt nichts.
    ratios_interval_s: Annotated[float, Field(ge=60)] = 300.0
    ratio_period: Literal["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"] = "5m"


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
    # Mindestabstand zwischen zwei Coverage-Refreshes (je Refresh ein Snapshot, Gewicht 20)
    coverage_refresh_min_interval_s: Annotated[float, Field(ge=0)] = 5.0
    # Liquidationen des Gesamtmarkts (alle Coins, sekündlich) statt nur der eigenen Symbole
    liquidations_all_market: bool = True
    # 1m-Kerzen-Historie beim Start (max. 1500 = 25 h; Gewicht 10 je Symbol)
    candles_1m_history: Annotated[int, Field(ge=1, le=1500)] = 1500
    # TradFi-Kontrakte (Gold, Silber, …) haben Handelspausen: dann kommen minutenlang keine
    # Orderbuch-Updates. Längere Frist, sonst würde ständig neu verbunden.
    tradfi_stale_timeout_s: Annotated[float, Field(gt=0)] = 900.0
    # Alle TradFi-Kontrakte (Aktien, ETFs, Rohstoffe, Devisen) automatisch aus der
    # exchangeInfo übernehmen – zusätzlich zu ``symbols``. Orderbuch erst bei Auswahl.
    tradfi_auto_discover: bool = True
    # Kategorie erzwingen (Symbol -> stock | etf | commodity | fx). Nötig für ETFs:
    # Binance kennzeichnet sie in der exchangeInfo nicht sicher von Aktien unterscheidbar.
    tradfi_categories: dict[str, str] = Field(default_factory=dict)
    validation: BookValidationConfig = Field(default_factory=BookValidationConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)


type ExchangeConfig = BinanceUsdmConfig


class AggregationConfig(_Model):
    interval_ms: Annotated[int, Field(ge=10)] = 100  # eine Heatmap-Spalte
    # Langzeit-Heatmap: gröbere Spalten (Mittelwert über die Zeit) für weites Herauszoomen
    long_interval_ms: Annotated[int, Field(ge=200)] = 5000
    # Langzeit: ein Bucket = n feine Buckets (Wert = Mittel je feinem Bucket)
    long_bucket_factor: Annotated[int, Field(ge=1, le=16)] = 4
    # Langzeit: grobe Buckets je Seite um den Mid (viel breiter als die feine Spalte)
    long_half_range_buckets: Annotated[int, Field(gt=0, le=5000)] = 1500
    # Langzeit: so oft wird das Buch innerhalb eines Intervalls abgetastet
    long_sample_ms: Annotated[int, Field(ge=100)] = 500
    # Langzeit-Spalten auf Platte (data_dir/longterm), damit sie Neustarts überstehen
    long_persist: bool = True
    # Preis-Bucket automatisch je Symbol: Bucket ~ Mid * bucket_bps / 10000, auf ein
    # "rundes" Vielfaches der Tick-Größe gerundet (1, 2, 5, 10, 20, …), mindestens 1 Tick.
    # Damit sehen BTC (Tick 0.10) und XRP (Tick 0.0001) gleich "dicht" aus.
    bucket_bps: Annotated[float, Field(gt=0)] = 0.06
    # Höchstens so viele Buckets je Seite um den Mid pro Spalte (zugeschnitten auf den
    # bekannten Bereich des Orderbuchs)
    half_range_buckets: Annotated[int, Field(gt=0, le=2000)] = 600


class ServerConfig(_Model):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(gt=0, lt=65536)] = 8765
    # So viele Spalten bekommt ein neu verbundener Client als Historie (100 ms -> 5 min)
    history_columns: Annotated[int, Field(ge=0)] = 3000
    # Langzeit-Heatmap-Historie (5 s -> 4320 = 6 h)
    long_history_columns: Annotated[int, Field(ge=0, le=16000)] = 4320
    # Sendepuffer je Client; ein langsamer Client verliert die ältesten Frames
    client_queue_frames: Annotated[int, Field(gt=0)] = 2000

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # Die Bridge ist lokal gedacht; kein versehentliches Lauschen auf 0.0.0.0.
        if v != "localhost" and not ipaddress.ip_address(v).is_loopback:
            raise ValueError("server.host muss eine Loopback-Adresse sein (z. B. 127.0.0.1)")
        return v


class UiConfig(_Model):
    """Wird beim Verbinden an das Frontend geschickt."""

    # Auto-Recentering, sobald der Mid näher als dieser Anteil der Höhe am Rand ist
    recenter_margin: Annotated[float, Field(ge=0, lt=0.5)] = 0.15
    # Bubble-Radius in px = Wurzel(Gegenwert in 1000 USDT) x Faktor (Fläche ~ Gegenwert).
    # In USDT statt Basis-Menge, damit Bubbles über alle Coins vergleichbar sind.
    bubble_px_per_sqrt_kusd: Annotated[float, Field(gt=0)] = 1.2
    bubble_max_px: Annotated[float, Field(gt=0)] = 40.0
    # Anfangs-Preisbereich der Ansicht: so viele Buckets je Seite um den Mid
    initial_view_buckets: Annotated[int, Field(gt=0)] = 250


class HistoryConfig(_Model):
    """Lange Tageshistorie (Spot) für ATH, Zyklen, Jahresperformance."""

    enabled: bool = True
    spot_rest_url: str = "https://data-api.binance.vision"
    # Ersatz für Symbole ohne Spot-Markt (Gold/Silber als TradFi-Perps)
    futures_rest_url: str = "https://fapi.binance.com"
    start: str = "2017-01-01"  # frühestes Datum; Binance-Spot beginnt im August 2017
    refresh_s: Annotated[float, Field(ge=60)] = 3600.0
    # Pause zwischen nachgeladenen Symbolen (schont das REST-Gewichtslimit)
    pace_s: Annotated[float, Field(ge=0)] = 0.5


class RecorderConfig(_Model):
    data_dir: Path = Path("data")
    flush_interval_s: Annotated[float, Field(gt=0)] = 5.0
    zstd_level: Annotated[int, Field(ge=1, le=22)] = 9


class AppConfig(_Model):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    exchanges: list[ExchangeConfig] = Field(default_factory=list)
    aggregation: AggregationConfig = Field(default_factory=AggregationConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    recorder: RecorderConfig = Field(default_factory=RecorderConfig)


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)
