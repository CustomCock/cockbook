"""Langzeit-Heatmap auf Platte, damit sie Neustarts übersteht.

Ablage: ``<data_dir>/longterm/<exchange>/<SYMBOL>/<YYYY-MM-DD>.msgpack`` – die fertigen
``cc``-Frames (MessagePack) einfach hintereinander angehängt. Anhängen statt die ganze
Historie regelmäßig neu zu schreiben: ein Frame (~ein paar KB) alle 5 s kostet nichts,
und ein Absturz verliert höchstens den letzten Frame. Tagesdateien lassen sich nach
Ablauf der Aufbewahrung einfach löschen.

Jeder Frame trägt Bucket-Größe (``bt``) und Faktor (``f``). Ändert sich die automatisch
bestimmte Bucket-Größe nach einem Neustart (Preis stark gelaufen), passen alte Spalten
nicht mehr zur Preisachse und werden beim Laden übersprungen.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import msgpack

log = logging.getLogger(__name__)


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, UTC).strftime("%Y-%m-%d")


class LongTermArchive:
    def __init__(self, data_dir: Path, exchange: str, retention_ms: int) -> None:
        self.base = data_dir / "longterm" / exchange
        self.retention_ms = retention_ms

    def _dir(self, symbol: str) -> Path:
        return self.base / symbol.upper()

    def append(self, symbol: str, ts_ms: int, frame: bytes) -> None:
        d = self._dir(symbol)
        try:
            d.mkdir(parents=True, exist_ok=True)
            with (d / f"{_day(ts_ms)}.msgpack").open("ab") as f:
                f.write(frame)
        except OSError as e:  # Platte voll o. Ä.: Anzeige läuft trotzdem weiter
            log.warning("longterm archive write failed", extra={"symbol": symbol, "error": str(e)})

    def load(self, symbol: str, now_ms: int, bucket_ticks: int, factor: int) -> list[bytes]:
        """Frames der letzten ``retention_ms``, passend zu Bucket-Größe und Faktor,
        zeitlich sortiert und ohne Duplikate."""
        start = now_ms - self.retention_ms
        out: dict[int, bytes] = {}
        skipped = 0
        first_day = datetime.fromtimestamp(start / 1000, UTC).date()
        last_day = datetime.fromtimestamp(now_ms / 1000, UTC).date()
        day = first_day
        while day <= last_day:
            path = self._dir(symbol) / f"{day:%Y-%m-%d}.msgpack"
            day += timedelta(days=1)
            if not path.exists():
                continue
            try:
                unpacker = msgpack.Unpacker(path.open("rb"), raw=False)
                for obj in unpacker:
                    if not isinstance(obj, dict):
                        continue
                    ts = int(obj.get("ts", 0))
                    if ts < start or ts > now_ms:
                        continue
                    if obj.get("bt") != bucket_ticks or obj.get("f") != factor:
                        skipped += 1
                        continue
                    out[ts] = msgpack.packb(obj)
            except (OSError, ValueError, msgpack.UnpackException) as e:
                # Abgeschnittener letzter Frame nach Absturz: Rest der Datei bleibt nutzbar
                log.warning(
                    "longterm archive read problem", extra={"file": str(path), "error": str(e)}
                )
        if skipped:
            log.info(
                "longterm frames with other bucket size skipped",
                extra={"symbol": symbol, "skipped": skipped},
            )
        return [out[t] for t in sorted(out)]

    def prune(self, symbol: str, now_ms: int) -> None:
        """Tagesdateien löschen, die komplett älter als die Aufbewahrung sind."""
        keep_from = _day(now_ms - self.retention_ms)  # Tag mit dem ältesten gültigen Frame
        d = self._dir(symbol)
        if not d.exists():
            return
        for path in d.glob("*.msgpack"):
            if path.stem < keep_from:
                with contextlib.suppress(OSError):
                    path.unlink()


def frame_ts(frame: bytes) -> int:
    obj: Any = msgpack.unpackb(frame)
    return int(obj["ts"])
