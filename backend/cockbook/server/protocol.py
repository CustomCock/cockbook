"""Nachrichtenformat Backend -> Frontend.

Binär (MessagePack) für alle Daten, JSON nur für Steuer-Nachrichten.
Zeitstempel in Millisekunden (int): ns passen nicht verlustfrei in eine JS-Number.
Jeder Daten-Frame trägt ``sym``, damit das Frontend nach einem Symbolwechsel
verspätete Frames des alten Symbols sicher verwerfen kann.

Chart-Frames (nur an Clients, die das Symbol abonniert haben, mit Historie):

``col``  Heatmap-Spalte    {t, sym, ts, s: synced, b: base_bucket, q: float32-LE-Bytes}
``bbo``  Best Bid/Ask      {t, sym, ts, bb: ticks|None, ba: ticks|None}
``tr``   Trades            {t, sym, ts, x: [[side, vwap_ticks, qty, n], …]} (+1 Kauf, -1 Verk.)
``lq``   Liquidationen     {t, sym, ts, x: [[side, price_ticks, qty], …]} (+1 Short, -1 Long)

Kennzahl-Frames (an alle Clients, Seitenleiste zeigt alle Coins):

``mark`` {sym, ts, mp, ip, fr, nft}   Mark-/Indexpreis, Funding-Rate, nächstes Funding (ms)
``tick`` {sym, ts, last, open, high, low, vol, qvol}   24h rollierend
``oi``   {sym, ts, oi}                Open Interest (Basis-Währung)
``ls``   {sym, ts, p, long, short, ratio}   Long/Short-Ratio (Accounts) je Periode p
``tv``   {sym, ts, p, buy, sell, ratio}     Taker Buy/Sell-Volumen je Periode p
``liq``  {sym, ts, side, price, qty}  einzelne Liquidation (Gesamtmarkt, alle Coins)
``d1``   {sym, x: [[t, o, h, l, c, v], …]}  Tageskerzen (Spot) für ATH/Zyklen/YTD
``sess``  {sym: Markt, ts, session, start, end}  TradFi-Handelszeit (z. B. COMMODITY)
``sync`` {sym, ts, synced, resyncs, reason}  Orderbuch-Diagnose je Coin

Langzeit-Heatmap im Symbol-Kanal: ``cc`` = grobe Spalte {t, sym, ts, s, b, q, bb, ba}
(Buckets = ``long_bucket_factor`` feine Buckets, Mittelwert über ``long_interval_ms``).

Kerzen-Frames im Symbol-Kanal: ``kh`` = 1m-Historie (beim Abo), ``k`` = laufende 1m-Kerze
{sym, x: [t, o, h, l, c, v, closed]}. 3m/5m/10m baut das Frontend aus 1m.

Kennzahlen als float: reine Anzeige, keine Orderbuch-Keys.

``q`` als rohe float32-Bytes statt Liste: 4 Byte/Bucket statt ~5–9 Byte msgpack-float,
und das Frontend lädt sie direkt als ``Float32Array`` in die GPU-Textur.
"""

from __future__ import annotations

import json
import sys
from array import array
from typing import Any

import msgpack

from cockbook.core.aggregation import HeatmapColumn
from cockbook.core.events import (
    CandleBar,
    Event,
    Liquidation,
    LongShortRatio,
    MarketSession,
    MarkPrice,
    OpenInterest,
    Side,
    TakerVolume,
    Ticker24h,
)

_NS_PER_MS = 1_000_000


def _ms(ns: int | None) -> int | None:
    return ns // _NS_PER_MS if ns is not None else None


def _f32_le(values: array[float]) -> bytes:
    if sys.byteorder == "big":
        values = array("f", values)
        values.byteswap()
    return values.tobytes()


def _pack(obj: dict[str, Any]) -> bytes:
    out: bytes = msgpack.packb(obj)
    return out


def encode_column(symbol: str, col: HeatmapColumn) -> list[bytes]:
    ts = col.ts_ns // _NS_PER_MS
    frames = [
        _pack(
            {
                "t": "col",
                "sym": symbol,
                "ts": ts,
                "s": col.synced,
                "b": col.base_bucket,
                "q": _f32_le(col.values),
            }
        ),
        _pack({"t": "bbo", "sym": symbol, "ts": ts, "bb": col.best_bid, "ba": col.best_ask}),
    ]
    if col.trades:
        frames.append(
            _pack(
                {
                    "t": "tr",
                    "sym": symbol,
                    "ts": ts,
                    "x": [
                        [1 if b.side is Side.BID else -1, b.vwap_ticks, b.qty, b.count]
                        for b in col.trades
                    ],
                }
            )
        )
    if col.liquidations:
        frames.append(
            _pack(
                {
                    "t": "lq",
                    "sym": symbol,
                    "ts": ts,
                    "x": [
                        [1 if side is Side.BID else -1, price, qty]
                        for side, price, qty in col.liquidations
                    ],
                }
            )
        )
    return frames


def encode_long_column(symbol: str, col: HeatmapColumn) -> bytes:
    return _pack(
        {
            "t": "cc",
            "sym": symbol,
            "ts": col.ts_ns // _NS_PER_MS,
            "s": col.synced,
            "b": col.base_bucket,
            "q": _f32_le(col.values),
            "bb": col.best_bid,
            "ba": col.best_ask,
        }
    )


def encode_session(e: MarketSession) -> bytes:
    return _pack(
        {
            "t": "sess",
            "sym": e.symbol,
            "ts": _ms(e.ts_ns),
            "session": e.session,
            "start": _ms(e.start_ns),
            "end": _ms(e.end_ns),
        }
    )


def encode_stat(e: Event) -> tuple[str, bytes] | None:
    """Kennzahl-Event -> (Art, Frame). None, wenn das Event keine Kennzahl ist."""
    base: dict[str, Any] = {"sym": e.symbol, "ts": _ms(e.ts_ns)}
    if isinstance(e, MarkPrice):
        kind = "mark"
        base |= {
            "mp": float(e.mark_price),
            "ip": float(e.index_price) if e.index_price is not None else None,
            "fr": float(e.funding_rate),
            "nft": _ms(e.next_funding_ns),
        }
    elif isinstance(e, Ticker24h):
        kind = "tick"
        base |= {
            "last": float(e.last),
            "open": float(e.open),
            "high": float(e.high),
            "low": float(e.low),
            "vol": float(e.volume),
            "qvol": float(e.quote_volume),
        }
    elif isinstance(e, OpenInterest):
        kind = "oi"
        base["oi"] = float(e.open_interest)
    elif isinstance(e, LongShortRatio):
        kind = "ls"
        base |= {
            "p": e.period,
            "long": float(e.long_share),
            "short": float(e.short_share),
            "ratio": float(e.ratio),
        }
    elif isinstance(e, TakerVolume):
        kind = "tv"
        base |= {
            "p": e.period,
            "buy": float(e.buy_volume),
            "sell": float(e.sell_volume),
            "ratio": float(e.ratio),
        }
    else:
        return None
    return kind, _pack({"t": kind, **base})


def encode_liquidation(e: Liquidation) -> bytes:
    return _pack(
        {
            "t": "liq",
            "sym": e.symbol,
            "ts": _ms(e.ts_ns),
            "side": 1 if e.side is Side.BID else -1,
            "price": float(e.price),
            "qty": float(e.qty),
        }
    )


def _bar(b: CandleBar) -> list[float | int]:
    return [
        b.open_time_ns // _NS_PER_MS,
        float(b.open),
        float(b.high),
        float(b.low),
        float(b.close),
        float(b.volume),
    ]


def encode_bars(kind: str, symbol: str, bars: list[CandleBar]) -> bytes:
    """``kind``: "kh" (1m-Historie) oder "d1" (Tageskerzen)."""
    return _pack({"t": kind, "sym": symbol, "x": [_bar(b) for b in bars]})


def encode_live_candle(symbol: str, bar: CandleBar, closed: bool) -> bytes:
    return _pack({"t": "k", "sym": symbol, "x": [*_bar(bar), closed]})


def encode_sync(symbol: str, ts_ns: int, synced: bool, resyncs: int, reason: str) -> bytes:
    return _pack(
        {
            "t": "sync",
            "sym": symbol,
            "ts": _ms(ts_ns),
            "synced": synced,
            "resyncs": resyncs,
            "reason": reason,
        }
    )


def encode_control(msg_type: str, **fields: Any) -> str:
    return json.dumps({"type": msg_type, **fields}, default=str)
