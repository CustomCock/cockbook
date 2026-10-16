"""Lange Tageshistorie (Spot) für Allzeithoch, Zyklen und Jahresperformance.

Quelle: Binance Spot ``GET /api/v3/klines`` (Doku gelesen am 2026-10-16:
https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints).
Gewicht 2, max. 1000 Kerzen je Abfrage, ``startTime``-Paginierung. Standard-Host
``data-api.binance.vision`` – laut Doku der Endpoint nur für öffentliche Marktdaten.

Warum Spot und nicht Futures? USDⓈ-M-Futures gibt es bei Binance erst seit 2019; Spot
reicht bis 2017 zurück (BTC ab 17.08.2017) und deckt damit mehr Marktzyklen ab. Für
Hochs/Tiefs auf Tagesbasis ist der Unterschied Spot/Perp vernachlässigbar.

Zwischenspeicher: ``<data_dir>/history/binance_spot/<SYMBOL>_1d.json`` – sonst würde
jeder Start ~4 Abfragen je Symbol kosten. Danach wird nur das Ende aktualisiert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        self.dir = data_dir / "history" / "binance_spot"
        self.bus = bus
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _path(self, symbol: str) -> Path:
        return self.dir / f"{symbol}_1d.json"

    def _load_cache(self, symbol: str) -> list[list[Any]]:
        try:
            rows: list[list[Any]] = json.loads(self._path(symbol).read_text(encoding="utf-8"))
            return rows
        except (OSError, ValueError):
            return []

    def _save_cache(self, symbol: str, rows: list[list[Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(symbol).with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        tmp.replace(self._path(symbol))  # atomar: nie eine halb geschriebene Datei

    async def _fetch_from(
        self, session: aiohttp.ClientSession, symbol: str, start_ms: int
    ) -> list[list[Any]]:
        url = f"{self.cfg.spot_rest_url}/api/v3/klines"
        rows: list[list[Any]] = []
        while not self._stop.is_set():
            params: dict[str, str | int] = {
                "symbol": symbol,
                "interval": "1d",
                "startTime": start_ms,
                "limit": 1000,
            }
            page, _ = await get_json(session, url, params, attempts=3)
            if not page:
                break
            rows.extend(page)
            if len(page) < 1000:
                break
            start_ms = int(page[-1][0]) + _DAY_MS
        return rows

    async def _update(self, session: aiohttp.ClientSession, symbol: str) -> None:
        cached = self._load_cache(symbol)
        if cached:
            # Die letzten Tage neu laden: die heutige Kerze läuft noch
            start = int(cached[-1][0]) - 2 * _DAY_MS
        else:
            start = int(
                datetime.fromisoformat(self.cfg.start).replace(tzinfo=UTC).timestamp() * 1000
            )
        try:
            new = await self._fetch_from(session, symbol, start)
        except RestError as e:
            log.warning("daily history failed", extra={"symbol": symbol, "error": str(e)})
            new = []
        rows = merge_rows(cached, new)
        if new:
            self._save_cache(symbol, rows)
        if rows:
            await self.bus.publish(
                CandleHistory(
                    exchange=EXCHANGE,
                    symbol=symbol,
                    ts_recv_ns=time.time_ns(),
                    interval="1d",
                    source="spot",
                    bars=tuple(parse_kline_row(r) for r in rows),
                )
            )
            log.info(
                "daily history ready",
                extra={"symbol": symbol, "days": len(rows), "fetched": len(new)},
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
