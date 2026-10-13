"""Strukturiertes Logging auf Basis der Standardbibliothek.

Zusatzfelder werden über ``extra={...}`` übergeben und landen als Key/Value im Output:

    log.info("resync", extra={"exchange": "binance", "symbol": "BTCUSDT"})

``console``: lesbar fürs Terminal. ``json``: eine JSON-Zeile pro Eintrag (für Dateien
und spätere Auswertung). Bewusst ohne Zusatz-Dependency wie structlog.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from cockbook.config import LoggingConfig

# Attribute, die jeder LogRecord hat – alles andere kam über ``extra``.
_STD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _STD_ATTRS}


def _ts(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": _ts(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        kv = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        line = f"{_ts(record)} {record.levelname:<7} {record.name}: {record.getMessage()}"
        if kv:
            line = f"{line} | {kv}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def setup_logging(cfg: LoggingConfig) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if cfg.format == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(cfg.level)
