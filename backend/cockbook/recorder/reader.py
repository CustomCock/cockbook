"""Lesen der Aufzeichnungen (für Tests, Analyse und ab Phase 4 das Replay)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard


@dataclass(frozen=True, slots=True)
class Record:
    recv_ns: int
    stream: str
    data: Any


def iter_records(path: Path) -> Iterator[Record]:
    with path.open("rb") as f:
        reader = zstandard.ZstdDecompressor().stream_reader(f, read_across_frames=True)
        for line in io.TextIOWrapper(reader, encoding="utf-8"):
            if not line.strip():
                continue
            obj = json.loads(line)
            yield Record(obj["t"], obj["s"], obj["d"])
