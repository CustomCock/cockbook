import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from cockbook.config import AppConfig, LoggingConfig, load_config
from cockbook.log import JsonFormatter, setup_logging

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path", ["config.yaml", "backend/cockbook/sim/config.sim.yaml"])
def test_repo_configs_are_valid(path: str) -> None:
    cfg = load_config(REPO_ROOT / path)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.aggregation.interval_ms > 0


def test_empty_file_gives_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(p) == AppConfig()


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"aggregation": {"intervall_ms": 100}})


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"logging": {"level": "VERBOSE"}})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_server_must_be_loopback(host: str) -> None:
    with pytest.raises(ValidationError, match="Loopback"):
        AppConfig.model_validate({"server": {"host": host}})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_allowed(host: str) -> None:
    assert AppConfig.model_validate({"server": {"host": host}}).server.host == host


def test_json_formatter_includes_extras() -> None:
    rec = logging.makeLogRecord(
        {"name": "x", "levelname": "INFO", "msg": "hi", "exchange": "binance"}
    )
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "hi"
    assert out["exchange"] == "binance"


def test_setup_logging_sets_level() -> None:
    setup_logging(LoggingConfig(level="WARNING"))
    assert logging.getLogger().level == logging.WARNING
