"""Lange Tageshistorie (Spot) für Allzeithoch, Zyklen und Jahresperformance.

Quelle: Binance Spot ``GET /api/v3/klines`` (Doku gelesen am 2026-10-16:
https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints).
Gewicht 2, max. 1000 Kerzen je Abfrage, ``startTime``-Paginierung. Standard-Host
``data-api.binance.vision`` – laut Doku der Endpoint nur für öffentliche Marktdaten.

Warum Spot und nicht Futures? USDⓈ-M-Futures gibt es bei Binance erst seit 2019; Spot
reicht bis 2017 zurück (BTC ab 17.08.2017) und deckt damit mehr Marktzyklen ab. Für
Hochs/Tiefs auf Tagesbasis ist der Unterschied Spot/Perp vernachlässigbar.

Ersatzquelle: Für Symbole ohne Spot-Markt (z. B. Gold ``XAUUSDT``, Silber ``XAGUSDT`` –
TradFi-Perps) antwortet Spot mit HTTP 400 "Invalid symbol". Dann werden die
Futures-Tageskerzen (``/fapi/v1/klines``) genommen; deren Historie ist kürzer.

Zwischenspeicher: ``<data_dir>/history/binance_<quelle>/<SYMBOL>_1d.json`` – sonst würde
jeder Start ~4 Abfragen je Symbol kosten. Danach wird nur das Ende aktualisiert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import aiohttp

from cockbook.adapters.binance import EXCHANGE, parse_kline_row
from cockbook.adapters.common import RestError, get_json, sleep_or_stop
from cockbook.config import HistoryConfig
from cockbook.core.eventbus import EventBus
from cockbook.core.events import CandleHistory

log = logging.getLogger(__name__)

_DAY_MS = 86_400_000


def merge_rows(old: list[list[Any]], new: list[list[Any]]) -> list[list[Any]]:
    """Vereinigt Kline-Zeilen nach Öffnungszeit; neuere Daten gewinnen (laufende Kerze)."""
    by_time = {int(r[0]): r for r in old}
    for r in new:
        by_time[int(r[0])] = r
    return [by_time[t] for t in sorted(by_time)]


class BinanceDailyHistory:
    name = f"{EXCHANGE}:history"

    def __init__(
        self, cfg: HistoryConfig, symbols: list[str], data_dir: Path, bus: EventBus
    ) -> None:
        self.cfg = cfg
        self.symbols = symbols
        self.base_dir = data_dir / "history"
        self.bus = bus
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    _SOURCES: ClassVar[dict[str, tuple[str, int]]] = {
        # Quelle -> (Pfad, max. Kerzen je Abfrage)
        "spot": ("/api/v3/klines", 1000),
        "futures": ("/fapi/v1/klines", 1000),
    }

    def _path(self, symbol: str, source: str) -> Path:
        return self.base_dir / f"binance_{source}" / f"{symbol}_1d.json"

    def _load_cache(self, symbol: str, source: str) -> list[list[Any]]:
        try:
            text = self._path(symbol, source).read_text(encoding="utf-8")
            rows: list[list[Any]] = json.loads(text)
            return rows
        except (OSError, ValueError):
            return []

    def _save_cache(self, symbol: str, source: str, rows: list[list[Any]]) -> None:
        path = self._path(symbol, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        tmp.replace(path)  # atomar: nie eine halb geschriebene Datei

    async def _fetch_from(
        self, session: aiohttp.ClientSession, symbol: str, start_ms: int, source: str = "spot"
    ) -> list[list[Any]]:
        path, limit = self._SOURCES[source]
        base = self.cfg.spot_rest_url if source == "spot" else self.cfg.futures_rest_url
        rows: list[list[Any]] = []
        while not self._stop.is_set():
            params: dict[str, str | int] = {
                "symbol": symbol,
                "interval": "1d",
                "startTime": start_ms,
                "limit": limit,
            }
            page, _ = await get_json(session, f"{base}{path}", params, attempts=3)
            if not page:
                break
            rows.extend(page)
            if len(page) < limit:
                break
            start_ms = int(page[-1][0]) + _DAY_MS
        return rows

    def _start_ms(self, cached: list[list[Any]]) -> int:
        if cached:
            # Die letzten Tage neu laden: die heutige Kerze läuft noch
            return int(cached[-1][0]) - 2 * _DAY_MS
        return int(datetime.fromisoformat(self.cfg.start).replace(tzinfo=UTC).timestamp() * 1000)

    async def _update(self, session: aiohttp.ClientSession, symbol: str) -> None:
        # Quelle: was schon im Cache liegt, sonst erst Spot, bei "Invalid symbol" Futures
        source = "futures" if self._load_cache(symbol, "futures") else "spot"
        cached = self._load_cache(symbol, source)
        try:
            new = await self._fetch_from(session, symbol, self._start_ms(cached), source)
        except RestError as e:
            if source == "spot" and not cached and "HTTP 400" in str(e):
                source = "futures"
                cached = []
                try:
                    new = await self._fetch_from(session, symbol, self._start_ms([]), source)
                except RestError as e2:
                    log.warning("daily history failed", extra={"symbol": symbol, "error": str(e2)})
                    new = []
            else:
                log.warning("daily history failed", extra={"symbol": symbol, "error": str(e)})
                new = []
        rows = merge_rows(cached, new)
        if new:
            self._save_cache(symbol, source, rows)
        if rows:
            await self.bus.publish(
                CandleHistory(
                    exchange=EXCHANGE,
                    symbol=symbol,
                    ts_recv_ns=time.time_ns(),
                    interval="1d",
                    source=source,
                    bars=tuple(parse_kline_row(r) for r in rows),
                )
            )
            log.info(
                "daily history ready",
                extra={"symbol": symbol, "source": source, "days": len(rows), "fetched": len(new)},
            )

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                for sym in self.symbols:
                    if self._stop.is_set():
                        return
                    await self._update(session, sym)
                if await sleep_or_stop(self._stop, self.cfg.refresh_s):
                    return
