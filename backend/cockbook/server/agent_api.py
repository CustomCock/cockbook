"""Abfragen für Claude (MCP-Erweiterung) – kompakte Zusammenfassungen statt Rohdaten.

Ein Sprachmodell kann mit 8000 Orderbuch-Leveln nichts anfangen (und es wäre teuer).
Darum liefert diese Schicht verdichtete Antworten: Wände (größte Level), Summen und
Ungleichgewicht in einem Preisbereich, Kauf-/Verkaufsvolumen und Delta eines Zeitfensters,
Kerzen in einem gewählten Intervall. Alles nur lesend.

Nachricht: ``{"type":"query","what":"book","symbol":"BTCUSDT","req":1, ...}``
Antwort:   ``{"type":"query_result","req":1,"ok":true,"data":{...}}``
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from cockbook.config import AgentConfig
from cockbook.core.events import Side

if TYPE_CHECKING:
    from cockbook.server.feed import MarketFeed

_MINUTES = (1, 3, 5, 15, 30, 60, 240)
_MAX_CANDLES = 1000
_MAX_TRADE_WINDOW_S = 24 * 3600
_TOP_TRADES = 10


class QueryError(ValueError):
    pass


def _num(x: Decimal | float) -> float:
    return float(x)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, Side):
        return v.name.lower()
    return v


class AgentApi:
    def __init__(self, feed: MarketFeed, cfg: AgentConfig) -> None:
        self.feed = feed
        self.cfg = cfg
        self._ops: dict[str, Callable[[dict[str, Any]], Any]] = {
            "symbols": self.symbols,
            "book": self.book,
            "trades": self.trades,
            "candles": self.candles,
            "stats": self.stats,
        }

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        what = msg.get("what")
        req = msg.get("req")
        fn = self._ops.get(what) if isinstance(what, str) else None
        if fn is None:
            return {
                "type": "query_result",
                "req": req,
                "ok": False,
                "message": f"unbekannte Abfrage: {what} (möglich: {', '.join(self._ops)})",
            }
        try:
            data = fn(msg)
        except QueryError as e:
            return {"type": "query_result", "req": req, "ok": False, "message": str(e)}
        return {"type": "query_result", "req": req, "ok": True, "data": data}

    # --- Hilfen ---------------------------------------------------------------------------

    def _legs(self, msg: dict[str, Any]) -> tuple[str, list[str]]:
        sym = msg.get("symbol")
        if not isinstance(sym, str) or not sym:
            raise QueryError("symbol fehlt (z. B. BTCUSDT, BITUNIX:BTCUSDT, MULTI:BTCUSDT)")
        key = sym.split("@", 1)[0]
        legs = self.feed.legs(key)
        if not legs:
            raise QueryError(f"unbekanntes Symbol: {sym} (Liste: Abfrage 'symbols')")
        return key, legs

    @staticmethod
    def _float(msg: dict[str, Any], name: str, default: float, lo: float, hi: float) -> float:
        v = msg.get(name, default)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            raise QueryError(f"{name} muss zwischen {lo} und {hi} liegen")
        return float(v)

    # --- Abfragen -------------------------------------------------------------------------

    def symbols(self, _msg: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for key, agg in sorted(self.feed.aggs.items()):
            inst = agg.instrument
            if inst is None:
                continue
            bb, ba = agg.book.best_bid(), agg.book.best_ask()
            row: dict[str, Any] = {
                "symbol": key,
                "synced": agg.synced,
                "tick_size": float(inst.tick_size),
            }
            if bb and ba:
                row["bid"] = _num(bb.price_ticks * inst.tick_size)
                row["ask"] = _num(ba.price_ticks * inst.tick_size)
            out.append(row)
        return out

    def book(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Orderbuch-Zusammenfassung im Bereich ± range_pct um den Mid (alle Beine zusammen)."""
        key, legs = self._legs(msg)
        rng = self._float(msg, "range_pct", self.cfg.book_range_pct, 0.01, 50) / 100
        n_walls = int(self._float(msg, "walls", self.cfg.book_walls, 1, 100))
        group = msg.get("group")  # Preis-Raster für Wände, z. B. 10 (USDT); None = Tick
        if group is not None and (not isinstance(group, (int, float)) or group <= 0):
            raise QueryError("group muss > 0 sein")
        # Alle Beine auf exakte Preise (Decimal) bringen: verschiedene Tick-Größen je Börse
        levels: dict[Side, dict[Decimal, Decimal]] = {Side.BID: {}, Side.ASK: {}}
        best_bid: Decimal | None = None
        best_ask: Decimal | None = None
        synced = True
        for leg in legs:
            agg = self.feed.aggs[leg]
            inst = agg.instrument
            if inst is None:
                continue
            synced = synced and agg.synced
            bb, ba = agg.book.best_bid(), agg.book.best_ask()
            if bb is None or ba is None:
                continue
            pb, pa = bb.price_ticks * inst.tick_size, ba.price_ticks * inst.tick_size
            best_bid = pb if best_bid is None else max(best_bid, pb)
            best_ask = pa if best_ask is None else min(best_ask, pa)
            for side in (Side.BID, Side.ASK):
                tgt = levels[side]
                for p, q in agg.book.side(side).items():
                    price = p * inst.tick_size
                    tgt[price] = tgt.get(price, Decimal(0)) + q
        if best_bid is None or best_ask is None:
            raise QueryError(f"{key}: kein Orderbuch (noch nicht synchron oder nicht abonniert)")
        mid = (best_bid + best_ask) / 2
        lo = mid * Decimal(1 - rng)
        hi = mid * Decimal(1 + rng)
        gstep = Decimal(str(group)) if group is not None else None

        def side_summary(side: Side) -> dict[str, Any]:
            inside = {p: q for p, q in levels[side].items() if lo <= p <= hi}
            if gstep is not None:
                grouped: dict[Decimal, Decimal] = {}
                for p, q in inside.items():
                    g = (p / gstep).to_integral_value(rounding="ROUND_FLOOR") * gstep
                    grouped[g] = grouped.get(g, Decimal(0)) + q
                inside = grouped
            qty = sum(inside.values(), Decimal(0))
            usd = sum((p * q for p, q in inside.items()), Decimal(0))
            walls = sorted(inside.items(), key=lambda x: x[1], reverse=True)[:n_walls]
            return {
                "qty": _num(qty),
                "usd": round(_num(usd)),
                "levels": len(inside),
                "walls": [
                    {
                        "price": _num(p),
                        "qty": _num(q),
                        "usd": round(_num(p * q)),
                        "dist_pct": round(_num((p - mid) / mid * 100), 3),
                    }
                    for p, q in walls
                ],
            }

        bids, asks = side_summary(Side.BID), side_summary(Side.ASK)
        tot = bids["usd"] + asks["usd"]
        return {
            "symbol": key,
            "venues": legs,
            "synced": synced,
            "best_bid": _num(best_bid),
            "best_ask": _num(best_ask),
            "mid": _num(mid),
            "spread": _num(best_ask - best_bid),
            "range_pct": rng * 100,
            "group": group,
            "bids": bids,
            "asks": asks,
            # +1 = nur Kaufliquidität im Bereich, -1 = nur Verkaufsliquidität
            "imbalance": round((bids["usd"] - asks["usd"]) / tot, 4) if tot else 0.0,
        }

    def trades(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Kauf-/Verkaufsvolumen (Aggressor), Delta, VWAP und größte Trades der letzten Sekunden."""
        key, legs = self._legs(msg)
        window = self._float(msg, "seconds", 300, 1, _MAX_TRADE_WINDOW_S)
        since_ns = time.time_ns() - int(window * 1e9)
        buy = sell = notional = qty_sum = Decimal(0)
        count = 0
        oldest: int | None = None
        big: list[tuple[Decimal, dict[str, Any]]] = []
        per_venue: dict[str, float] = {}
        for leg in legs:
            agg = self.feed.aggs[leg]
            inst = agg.instrument
            tape = self.feed.tape.get(leg)
            if inst is None or not tape:
                continue
            vdelta = Decimal(0)
            oldest = tape[0].ts_ns if oldest is None else min(oldest, tape[0].ts_ns)
            for t in reversed(tape):
                if t.ts_ns < since_ns:
                    break
                price = t.price_ticks * inst.tick_size
                if t.aggressor is Side.BID:
                    buy += t.qty
                    vdelta += t.qty
                else:
                    sell += t.qty
                    vdelta -= t.qty
                qty_sum += t.qty
                notional += price * t.qty
                count += 1
                big.append(
                    (
                        price * t.qty,
                        {
                            "ts_ms": t.ts_ns // 1_000_000,
                            "price": _num(price),
                            "qty": _num(t.qty),
                            "side": "buy" if t.aggressor is Side.BID else "sell",
                            "venue": leg,
                        },
                    )
                )
            per_venue[leg] = _num(vdelta)
        big.sort(key=lambda x: x[0], reverse=True)
        covered = (time.time_ns() - oldest) / 1e9 if oldest is not None else 0.0
        return {
            "symbol": key,
            "seconds": window,
            # Puffer reicht evtl. nicht so weit zurück (tape_trades in config.yaml)
            "covered_seconds": round(min(window, covered), 1),
            "count": count,
            "buy_qty": _num(buy),
            "sell_qty": _num(sell),
            "delta_qty": _num(buy - sell),
            "delta_by_venue": per_venue,
            "vwap": _num(notional / qty_sum) if qty_sum else None,
            "notional_usd": round(_num(notional)),
            "largest": [x for _, x in big[:_TOP_TRADES]],
        }

    def candles(self, msg: dict[str, Any]) -> dict[str, Any]:
        key, _legs = self._legs(msg)
        minutes = msg.get("minutes", 1)
        if minutes not in _MINUTES:
            raise QueryError(f"minutes muss eines von {_MINUTES} sein")
        limit = int(self._float(msg, "limit", 100, 1, _MAX_CANDLES))
        bars = self.feed.candles_1m(key)
        step = int(minutes) * 60_000_000_000
        out: list[list[float]] = []
        for b in bars:
            t = b.open_time_ns - b.open_time_ns % step
            if out and out[-1][0] == t // 1_000_000:
                row = out[-1]
                row[2] = max(row[2], _num(b.high))
                row[3] = min(row[3], _num(b.low))
                row[4] = _num(b.close)
                row[5] += _num(b.volume)
            else:
                out.append(
                    [
                        t // 1_000_000,
                        _num(b.open),
                        _num(b.high),
                        _num(b.low),
                        _num(b.close),
                        _num(b.volume),
                    ]
                )
        return {
            "symbol": key,
            "minutes": minutes,
            "columns": ["t_ms", "open", "high", "low", "close", "volume"],
            "candles": out[-limit:],
        }

    def stats(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Letzte Kennzahlen: 24h-Ticker, Mark-Preis/Funding, Open Interest, Long/Short …"""
        key, legs = self._legs(msg)
        out: dict[str, Any] = {}
        for leg in legs:
            per: dict[str, Any] = {}
            for name, ev in self.feed.stats.get(leg, {}).items():
                d = {
                    k: _jsonable(v)
                    for k, v in dataclasses.asdict(ev).items()
                    if k not in ("exchange", "symbol", "ts_recv_ns", "ts_exchange_ns")
                }
                d["age_s"] = round((time.time_ns() - ev.ts_ns) / 1e9, 1)
                per[name] = d
            out[leg] = per
        return {"symbol": key, "venues": out}
