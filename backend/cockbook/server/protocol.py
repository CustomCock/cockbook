"""Nachrichtenformat Backend -> Frontend.

Binär (MessagePack) für alles Hochfrequente, JSON nur für Steuer-Nachrichten.
Zeitstempel in Millisekunden (int): ns passen nicht verlustfrei in eine JS-Number.

Binär-Frames (Feld ``t`` = Typ):

``col``  Heatmap-Spalte   {t, ts, s: synced, b: base_bucket, q: float32-LE-Bytes}
``bbo``  Best Bid/Ask     {t, ts, bb: ticks|None, ba: ticks|None}
``tr``   Trades der Spalte {t, ts, x: [[side(+1 Kauf/-1 Verkauf), vwap_ticks, qty, count], …]}

Spalte, BBO und Trades desselben Intervalls haben denselben ``ts`` und kommen in
dieser Reihenfolge.

``q`` als rohe float32-Bytes statt Liste: 4 Byte/Bucket statt ~5–9 Byte msgpack-float,
und das Frontend kann sie direkt als ``Float32Array`` in die GPU-Textur laden.
"""

from __future__ import annotations

import json
import sys
from array import array
from typing import Any

import msgpack

from cockbook.core.aggregation import HeatmapColumn
from cockbook.core.events import Side

_NS_PER_MS = 1_000_000


def _f32_le(values: array[float]) -> bytes:
    if sys.byteorder == "big":
        values = array("f", values)
        values.byteswap()
    return values.tobytes()


def encode_column(col: HeatmapColumn) -> list[bytes]:
    ts = col.ts_ns // _NS_PER_MS
    frames = [
        msgpack.packb(
            {"t": "col", "ts": ts, "s": col.synced, "b": col.base_bucket, "q": _f32_le(col.values)}
        ),
        msgpack.packb({"t": "bbo", "ts": ts, "bb": col.best_bid, "ba": col.best_ask}),
    ]
    if col.trades:
        frames.append(
            msgpack.packb(
                {
                    "t": "tr",
                    "ts": ts,
                    "x": [
                        [1 if b.side is Side.BID else -1, b.vwap_ticks, b.qty, b.count]
                        for b in col.trades
                    ],
                }
            )
        )
    return frames


def encode_control(msg_type: str, **fields: Any) -> str:
    return json.dumps({"type": msg_type, **fields}, default=str)
