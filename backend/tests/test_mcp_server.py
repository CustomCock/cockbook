"""MCP-Server: Zeitangaben, Zeichnungs-Aufbau und Ende-zu-Ende über eine echte Bridge."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from cockbook import mcp_server as m
from cockbook.config import AgentConfig, ServerConfig
from cockbook.server.agent_api import AgentApi
from cockbook.server.bridge import Bridge
from cockbook.server.drawings import DrawingStore
from tests.test_agent_api import FakeFeed
from tests.test_server import free_port


def test_parse_time() -> None:
    assert m.parse_time(1_700_000_000_000) == 1_700_000_000_000
    assert m.parse_time(1_700_000_000) == 1_700_000_000_000  # Sekunden erkannt
    assert m.parse_time("2023-11-14T22:13:20Z") == 1_700_000_000_000
    assert m.parse_time("2023-11-14T22:13:20") == 1_700_000_000_000  # ohne Zone = UTC
    now = int(time.time() * 1000)
    assert abs(m.parse_time("-15m") - (now - 900_000)) < 2000
    assert abs(m.parse_time("now") - now) < 2000
    with pytest.raises(ValueError, match="nicht lesbar"):
        m.parse_time("gestern")


def test_build_drawing() -> None:
    d = m.build_drawing("hline", [{"time": "now", "price": 100}], color="#ff0000", text="Wand")
    assert d["tool"] == "hline"
    assert d["by"] == "claude"
    assert d["style"] == {"color": "#ff0000", "text": "Wand"}
    assert m.build_drawing("ray", [{"time": 1, "price": 1}] * 2)["style"]["extendRight"] is True
    with pytest.raises(ValueError, match="unbekanntes"):
        m.build_drawing("laser", [])
    with pytest.raises(ValueError, match="3 Punkte"):
        m.build_drawing("long", [{"time": 1, "price": 1}])
    with pytest.raises(ValueError, match="#rrggbb"):
        m.build_drawing("hline", [{"time": 1, "price": 1}], color="red")


def test_no_live_tools_registered() -> None:
    import asyncio

    tools = asyncio.run(m.mcp.list_tools())
    names = {t.name for t in tools}
    assert "add_drawing" in names
    assert "get_orderbook" in names
    assert not any("live" in n and n != "get_live_account" for n in names)
    by_name = {t.name: t for t in tools}
    ann = by_name["get_orderbook"].annotations
    assert ann is not None
    assert ann.read_only_hint is True
    assert by_name["place_paper_order"].meta == {"anthropic/requiresUserInteraction": True}


@pytest.fixture
async def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Bridge]:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port))
    bridge.draw_handler = DrawingStore(tmp_path, AgentConfig()).handle
    bridge.query_handler = AgentApi(FakeFeed(), AgentConfig()).handle  # type: ignore[arg-type]
    seen: list[dict[str, Any]] = []

    def paper(msg: dict[str, Any]) -> dict[str, Any]:
        seen.append(msg)
        return {"type": "paper_result", "req": msg.get("req"), "ok": True, "message": "ok"}

    bridge.paper_handler = paper
    bridge.set_shared("paper", json.dumps({"type": "paper_state", "account": {"wallet": 10000}}))
    live_called: list[Any] = []

    async def live(msg: dict[str, Any]) -> dict[str, Any]:
        live_called.append(msg)
        return {}

    bridge.live_handler = live
    await bridge.start()
    monkeypatch.setenv("COCKBOOK_URL", f"ws://127.0.0.1:{port}/ws")
    monkeypatch.setattr(m, "_client", None)
    bridge.paper_seen = seen  # type: ignore[attr-defined]
    bridge.live_seen = live_called  # type: ignore[attr-defined]
    try:
        yield bridge
    finally:
        await m.client().close()
        await bridge.stop()


async def test_end_to_end(backend: Bridge) -> None:
    syms = await m.list_symbols()
    assert {s["symbol"] for s in syms} == {"BTCUSDT", "BITUNIX:BTCUSDT"}
    book = await m.get_orderbook("BTCUSDT", range_pct=1)
    assert book["mid"] == 99.95
    flow = await m.get_trade_flow("BTCUSDT", seconds=60)
    assert flow["delta_qty"] == 1.5

    # Zeichnen auf Bitunix landet beim Coin (für alle Börsen)
    r = await m.add_drawing(
        "BITUNIX:BTCUSDT", "hline", [{"time": "now", "price": 99.0}], text="Bid-Wand"
    )
    items = await m.list_drawings("BTCUSDT")
    assert [d["id"] for d in items] == [r["id"]]
    assert items[0]["by"] == "claude"
    await m.update_drawing("BTCUSDT", r["id"], color="#55e5a8")
    assert (await m.list_drawings("BTCUSDT"))[0]["style"]["color"] == "#55e5a8"
    with pytest.raises(m.BackendError, match="Punkte"):
        await m.add_drawing("BTCUSDT", "trend", [{"time": "now", "price": 1}])
    assert (await m.clear_drawings("BTCUSDT"))["remaining"] == 0

    acc = await m.get_paper_account()
    assert acc["account"]["wallet"] == 10000
    assert await m.place_paper_order("BTCUSDT", "buy", 0.01, kind="limit", price=99.5) == "ok"
    sent = backend.paper_seen[-1]  # type: ignore[attr-defined]
    assert sent["op"] == "order"
    assert sent["qty"] == "0.01"
    assert sent["price"] == "99.5"

    # Sperre: live-Nachrichten verlassen den MCP-Server nie
    with pytest.raises(m.BackendError, match="gesperrt"):
        await m.client().request({"type": "live", "op": "order"}, {"live_result"})
    assert backend.live_seen == []  # type: ignore[attr-defined]


async def test_backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COCKBOOK_URL", f"ws://127.0.0.1:{free_port()}/ws")
    monkeypatch.setattr(m, "_client", None)
    with pytest.raises(m.BackendError, match="nicht erreichbar"):
        await m.list_symbols()
    await m.client().close()
