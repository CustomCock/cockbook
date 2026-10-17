"""Schlüsselbild-/Delta-Kodierung der Heatmap-Spalten und Historie ab Schlüsselbild."""

from __future__ import annotations

import asyncio
import json
import random
import socket
from array import array
from typing import Any

import aiohttp
import msgpack
import pytest

from cockbook.config import ServerConfig
from cockbook.core.aggregation import ColumnDeltaEncoder, HeatmapColumn
from cockbook.server.bridge import Bridge
from cockbook.server.protocol import encode_control


def col(
    ts: int, buckets: dict[int, float], synced: bool = True, dense: bool = False
) -> HeatmapColumn:
    keys = sorted(buckets)
    base = keys[0] if keys else 0
    if dense:
        vals = array("f", [0.0] * ((keys[-1] - base + 1) if keys else 0))
        for b in keys:
            vals[b - base] = buckets[b]
        return HeatmapColumn(ts, synced, base, vals, 1, 2, center=base)
    return HeatmapColumn(
        ts, synced, base, array("f", (buckets[b] for b in keys)), 1, 2,
        idx=array("i", (b - base for b in keys)), center=base,
    )  # fmt: skip


def apply(state: dict[int, float] | None, c: HeatmapColumn) -> dict[int, float] | None:
    """So rekonstruiert das Frontend: Schlüsselbild ersetzt, Delta ändert (0 = weg)."""
    if not c.synced:
        return None
    if c.keyframe:
        return dict(c.items())
    assert state is not None
    out = dict(state)
    assert c.idx is not None
    for i, v in zip(c.idx, c.values, strict=True):
        b = c.base_bucket + i
        if v:
            out[b] = v
        else:
            out.pop(b, None)
    return out


def test_deltas_reconstruct_every_column_exactly() -> None:
    rng = random.Random(7)
    enc = ColumnDeltaEncoder(keyframe_every=10)
    book = {b: float(rng.randint(1, 50)) for b in range(1000, 1400)}
    state: dict[int, float] | None = None
    seqs = []
    for t in range(200):
        # wenige Änderungen, Verschwinden, Neues, ab und zu ein Sprung des Fensters
        for _ in range(5):
            book[rng.randint(950, 1450)] = float(rng.randint(1, 50))
        for _ in range(2):
            book.pop(rng.choice(list(book)), None)
        if t == 120:
            book = {b + 300: v for b, v in book.items()}
        synced = t not in (60, 61)
        expected = dict(book) if synced else None
        c = enc.encode(col(t, book, synced, dense=t % 7 == 0))
        seqs.append(c.seq)
        state = apply(state, c)
        assert state == expected, t
    assert seqs == list(range(1, 201))


def test_keyframe_rules() -> None:
    enc = ColumnDeltaEncoder(keyframe_every=3)
    book = {b: 1.0 for b in range(100)}
    kinds = []
    for t in range(7):
        book[t] = 2.0 + t
        kinds.append(enc.encode(col(t, book)).keyframe)
    assert kinds == [True, False, False, True, False, False, True]
    # kleine Änderung -> nur 1 Bucket übertragen
    book[50] = 9.0
    c = enc.encode(col(8, book))
    assert not c.keyframe
    assert list(c.items()) == [(50, 9.0)]
    # unsynchron -> danach Schlüsselbild
    assert enc.encode(col(9, {}, synced=False)).keyframe
    assert enc.encode(col(10, book)).keyframe
    # mehr als die Hälfte geändert -> lieber Schlüsselbild
    assert enc.encode(col(11, {b + 1000: v for b, v in book.items()})).keyframe


def test_unchanged_column_is_empty_delta() -> None:
    enc = ColumnDeltaEncoder(keyframe_every=50)
    book = {5: 1.0, 9: 2.0}
    enc.encode(col(0, book))
    c = enc.encode(col(1, book))
    assert not c.keyframe
    assert len(c.values) == 0


def test_rejects_invalid_interval() -> None:
    with pytest.raises(ValueError, match="keyframe_every"):
        ColumnDeltaEncoder(0)


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def _receive_ts(port: int, sub: dict[str, Any], expect: int) -> list[int]:
    got: list[int] = []
    async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
        await ws.send_str(json.dumps(sub))
        async with asyncio.timeout(5):
            while len(got) < expect:
                msg = await ws.receive()
                if msg.type is aiohttp.WSMsgType.BINARY:
                    got.append(msgpack.unpackb(msg.data)["ts"])
    return got


async def test_bridge_history_starts_at_keyframe() -> None:
    port = _port()
    # Historie fasst 5 Spalten: 3..7 -> 3, 4 sind Deltas ohne ihr Schlüsselbild (0)
    bridge = Bridge(ServerConfig(port=port, history_columns=5))
    await bridge.start()
    try:
        bridge.register_symbol("X")
        bridge.set_hello("X", encode_control("hello", symbol="X"))
        for ts in range(8):
            bridge.publish("X", [msgpack.packb({"t": "col", "ts": ts})], ts, keyframe=ts % 5 == 0)
        assert await _receive_ts(port, {"type": "subscribe", "symbol": "X"}, 3) == [5, 6, 7]
        # since=6: ab dem Schlüsselbild 5 (Frontend braucht es für seinen Bucket-Zustand)
        got = await _receive_ts(port, {"type": "subscribe", "symbol": "X", "since": 6}, 3)
        assert got == [5, 6, 7]
    finally:
        await bridge.stop()
