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
    frames = encode_column(column(1_791_000_000_123))
    col, bbo, tr = (msgpack.unpackb(f) for f in frames)
    assert col["t"] == "col"
    assert col["ts"] == 1_791_000_000_123
    assert col["b"] == 1000
    assert struct.unpack("<3f", col["q"]) == (0.0, 1.5, 2.25)  # little-endian float32
    assert bbo == {"t": "bbo", "ts": 1_791_000_000_123, "bb": 5001, "ba": 5002}
    assert tr["x"] == [[-1, 5001.0, 2.0, 3]]


def test_no_trade_frame_without_trades() -> None:
    c = column(1)
    c.trades = []
    assert len(encode_column(c)) == 2


async def test_bridge_sends_hello_status_history_then_live() -> None:
    port = free_port()
    bridge = Bridge(ServerConfig(port=port, history_columns=2))
    await bridge.start()
    try:
        bridge.set_hello(encode_control("hello", symbol="BTCUSDT"))
        bridge.set_status(encode_control("status", synced=True))
        for i in range(3):  # Historie fasst 2 Intervalle
            bridge.publish(encode_column(column(i)))
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            got: list[Any] = []
            async with asyncio.timeout(5):
                while len(got) < 2 + 2 * 3 + 3:
                    if len(got) == 2 + 2 * 3:
                        bridge.publish(encode_column(column(99)))
                    msg = await ws.receive()
                    got.append(
                        json.loads(msg.data)
                        if msg.type is aiohttp.WSMsgType.TEXT
                        else msgpack.unpackb(msg.data)
                    )
        assert got[0]["type"] == "hello"
        assert got[1]["type"] == "status"
        assert [g["ts"] for g in got[2:] if g.get("t") == "col"] == [1, 2, 99]
    finally:
        await bridge.stop()


async def test_new_hello_clears_history() -> None:
    bridge = Bridge(ServerConfig(history_columns=10))
    bridge.set_hello("a")
    bridge.publish(encode_column(column(1)))
    bridge.set_hello("b")
    assert len(bridge._history) == 0
