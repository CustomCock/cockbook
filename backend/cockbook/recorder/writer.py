"""Rohdaten-Aufzeichnung: zstd-komprimiertes JSONL, eine Datei pro Stunde (UTC).

Pfad: ``<data_dir>/<exchange>/<symbol>/YYYY-MM-DDTHH.jsonl.zst``

Zeilenformat::

    {"t":<recv_ns>,"s":"<stream>","d":<Rohnachricht unverändert>}

Die Rohnachricht wird *verbatim* eingebettet (kein Parse/Re-Serialize): kein
Rundungs- oder Formatverlust, Replay sieht exakt die Bytes der Börse. Das geht,
weil ein gültiges JSON-Dokument als Wert in ein anderes eingesetzt werden kann.

Crash-Sicherheit: Jeder Flush schreibt einen *vollständigen* zstd-Frame und hängt ihn
an. zstd erlaubt beliebig viele aneinandergehängte Frames in einer Datei. Stürzt der
Prozess ab, sind höchstens die Daten seit dem letzten Flush verloren – die Datei
bleibt lesbar. (Ein einziger langer Stream-Frame wäre nach einem Absturz am Ende
kaputt.)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from cockbook.core.events import RawMessage

log = logging.getLogger(__name__)


def hour_key(recv_ns: int) -> str:
    # Ganzzahl-Division: recv_ns / 1e9 als float rundet 1 ns vor der vollen Stunde
    # bereits auf die nächste Stunde -> Datensatz in falscher Datei.
    return datetime.fromtimestamp(recv_ns // 1_000_000_000, UTC).strftime("%Y-%m-%dT%H")


def record_path(base: Path, exchange: str, symbol: str, recv_ns: int) -> Path:
    return base / exchange / symbol / f"{hour_key(recv_ns)}.jsonl.zst"


def encode_line(msg: RawMessage) -> str:
    # JSON-Whitespace außerhalb von Strings darf ersetzt werden; Zeilenumbrüche
    # innerhalb von JSON-Strings sind ohnehin escaped. So bleibt es eine Zeile.
    payload = msg.payload.replace("\r", " ").replace("\n", " ")
    return f'{{"t":{msg.ts_recv_ns},"s":{json.dumps(msg.stream)},"d":{payload}}}\n'


class RecordWriter:
    def __init__(self, base_dir: Path, *, zstd_level: int = 9) -> None:
        self.base_dir = base_dir
        self._cctx = zstandard.ZstdCompressor(level=zstd_level)
        self._pending: list[RawMessage] = []
        self.records_written = 0
        self.bytes_written = 0

    def add(self, msg: RawMessage) -> None:
        self._pending.append(msg)

    @property
    def pending(self) -> int:
        return len(self._pending)

    def take(self) -> list[RawMessage]:
        batch, self._pending = self._pending, []
        return batch

    def write_batch(self, batch: list[RawMessage]) -> None:
        """Blockierend (Kompression + Datei-I/O) – aus einem Thread aufrufen."""
        by_file: dict[Path, list[str]] = defaultdict(list)
        for msg in batch:
            by_file[record_path(self.base_dir, msg.exchange, msg.symbol, msg.ts_recv_ns)].append(
                encode_line(msg)
            )
        for path, lines in by_file.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            frame = self._cctx.compress("".join(lines).encode("utf-8"))
            with path.open("ab") as f:
                f.write(frame)
            self.records_written += len(lines)
            self.bytes_written += len(frame)
