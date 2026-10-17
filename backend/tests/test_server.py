import asyncio
import json
import socket
import struct
from array import array
from typing import Any

import aiohttp
import msgpack

from cockbook.config import ServerConfig
from cockbook.core.aggregation import HeatmapColumn, TradeBin
from cockbook.core.events import Side
from cockbook.server.bridge import Bridge
from cockbook.server.protocol import encode_column, encode_control


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def column(ts_ms: int) -> HeatmapColumn:
    return HeatmapColumn(
        ts_ms * 1_000_000,
        True,
        1000,
        array("f", [0.0, 1.5, 2.25]),
        5001,
        5002,
        [TradeBin(Side.ASK, 2.0, 2.0 * 5001, 3)],
    )


def test_column_frames_roundtrip() -> None:
    frames = encode_column("BTCUSDT", column(1_791_000_000_123))
    col, bbo, tr = (msgpack.unpackb(f) for f in frames)
    assert col["t"] == "col"
    assert col["sym"] == "BTCUSDT"
    assert col["ts"] == 1_791_000_000_123
    assert col["b"] == 1000
    assert struct.unpack("<3f", col["q"]) == (0.0, 1.5, 2.25)  # little-endian float32
    assert bbo == {"t": "bbo", "sym": "BTCUSDT", "ts": 1_791_000_000_123, "bb": 5001, "ba": 5002}
    assert tr["x"] == [[-1, 5001.0, 2.0, 3]]


def test_no_trade_frame_without_trades() -> None:
    c = column(1)
    c.trades = []
    assert len(encode_column("BTCUSDT", c)) == 2


async def test_bridge_sends_hello_status_history_then_live() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port, history_columns=2))
    await bridge.start()
    try:
        bridge.register_symbol("BTCUSDT")
        bridge.set_hello("BTCUSDT", encode_control("hello", symbol="BTCUSDT"))
        bridge.set_status("BTCUSDT", encode_control("status", synced=True))
        for i in range(3):  # Historie fasst 2 Intervalle
            bridge.publish("BTCUSDT", encode_column("BTCUSDT", column(i)))
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            got: list[Any] = []
            async with asyncio.timeout(5):
                while len(got) < 3 + 2 * 3 + 3:
                    if len(got) == 3 + 2 * 3:
                        bridge.publish("BTCUSDT", encode_column("BTCUSDT", column(99)))
                    msg = await ws.receive()
                    got.append(
                        json.loads(msg.data)
                        if msg.type is aiohttp.WSMsgType.TEXT
                        else msgpack.unpackb(msg.data)
                    )
        assert got[0]["type"] == "symbols"
        assert got[1]["type"] == "hello"
        assert got[2]["type"] == "status"
        assert [g["ts"] for g in got[3:] if g.get("t") == "col"] == [1, 2, 99]
    finally:
        await bridge.stop()


async def test_new_hello_clears_history() -> None:
    bridge = Bridge(ServerConfig(history_columns=10))
    bridge.set_hello("BTCUSDT", "a")
    bridge.publish("BTCUSDT", encode_column("BTCUSDT", column(1)))
    bridge.set_hello("BTCUSDT", "b")
    assert len(bridge._channels["BTCUSDT"].history) == 0


async def test_bridge_token_and_origin() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port), token="x" * 24)
    await bridge.start()
    url = f"http://127.0.0.1:{port}/ws"
    try:
        async with aiohttp.ClientSession() as s:
            # ohne / mit falschem Token: auth_error + Close 4401
            for q in ("", "?token=falsch"):
                async with s.ws_connect(url + q) as ws:
                    assert json.loads((await ws.receive()).data)["type"] == "auth_error"
                    assert (await ws.receive()).type is aiohttp.WSMsgType.CLOSE
                    assert ws.close_code == 4401
            # richtiges Token, aber fremde Seite (Origin) -> abgelehnt
            async with s.ws_connect(
                url + "?token=" + "x" * 24, origin="https://evil.example"
            ) as ws:
                assert json.loads((await ws.receive()).data)["type"] == "auth_error"
            # richtiges Token, gleiche Adresse -> Symbol-Liste kommt
            async with s.ws_connect(
                url + "?token=" + "x" * 24, origin=f"http://127.0.0.1:{port}"
            ) as ws:
                assert json.loads((await ws.receive()).data)["type"] == "symbols"
    finally:
        await bridge.stop()


def test_server_mode_requires_long_token(monkeypatch: Any) -> None:
    import pytest

    from cockbook.__main__ import server_mode
    from cockbook.config import AppConfig

    cfg = AppConfig()
    monkeypatch.delenv("COCKBOOK_PUBLIC", raising=False)
    assert server_mode(cfg) == (cfg, None)
    monkeypatch.setenv("COCKBOOK_PUBLIC", "1")
    monkeypatch.setenv("COCKBOOK_TOKEN", "kurz")
    with pytest.raises(SystemExit, match="mindestens"):
        server_mode(cfg)
    monkeypatch.setenv("COCKBOOK_TOKEN", "a" * 32)
    cfg2, token = server_mode(cfg)
    assert cfg2.server.host == "0.0.0.0"
    assert token == "a" * 32
