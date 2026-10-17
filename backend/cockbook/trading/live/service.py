"""Echtes Bitunix-Konto – Stufe 1: nur lesen.

Ablauf:
- Ohne ``BITUNIX_API_KEY``/``BITUNIX_API_SECRET`` passiert nichts (Status "keine Keys").
- REST-Stand (Konto, Positionsmodus, Positionen, Orders, TP/SL) beim Start, nach jedem
  Push des privaten WebSockets (gebündelt) und als Sicherheitsnetz alle ``refresh_s``.
  REST ist die Wahrheit, die Pushes sind nur der Anlass: Ein verlorener Push (Abbruch)
  kann so den Stand nicht still verfälschen.
- Der uPnL offener Positionen wird zwischen den Abgleichen mit dem Mid aus dem eigenen
  Orderbuch fortgeschrieben (Bitunix pusht Positionen nur bei Änderungen, nicht bei
  Kursbewegung).

Stufe 2 (Orders senden) baut hierauf auf; dieser Dienst sendet keine einzige Order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from decimal import Decimal
from typing import Any

import aiohttp

from cockbook.adapters.bitunix import ping_loop
from cockbook.adapters.common import run_ws
from cockbook.config import LiveConfig
from cockbook.core.aggregation import HeatmapAggregator
from cockbook.core.venue import venue_key
from cockbook.server.bridge import Bridge
from cockbook.trading.live.bitunix_client import (
    BitunixApiError,
    BitunixPrivateRest,
    Credentials,
    dec,
    ws_login_message,
)

log = logging.getLogger(__name__)

_DEBOUNCE_S = 0.3
_PUBLISH_S = 1.0
_PRIVATE_CHANNELS = ("balance", "position", "order", "tpsl")


def _f(v: Any) -> float | None:
    d = dec(v)
    return float(d) if d is not None else None


def build_state(
    account: dict[str, Any],
    position_mode: str,
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    tpsl: list[dict[str, Any]],
) -> dict[str, Any]:
    """REST-Antworten -> Zustand fürs Frontend (Symbole als CockBook-Keys)."""
    warnings: list[str] = []
    if position_mode and position_mode != "ONE_WAY":
        warnings.append(
            f"Konto steht auf {position_mode}: CockBook erwartet One-Way "
            "(in Bitunix unter Einstellungen → Positionsmodus umstellen)."
        )
    # Positions-TP/SL (eigene Orders bei Bitunix, je positionId)
    tp_by_pos: dict[str, dict[str, Any]] = {}
    for t in tpsl:
        pid = str(t.get("positionId", ""))
        cur = tp_by_pos.setdefault(pid, {})
        if t.get("tpPrice"):
            cur["tp"] = _f(t.get("tpPrice"))
        if t.get("slPrice"):
            cur["sl"] = _f(t.get("slPrice"))
    out_pos = []
    for p in positions:
        qty = dec(p.get("qty")) or Decimal(0)
        if p.get("side") == "SHORT":
            qty = -qty
        if p.get("marginMode") and p.get("marginMode") != "ISOLATION":
            warnings.append(f"{p.get('symbol')}: Margin-Modus {p.get('marginMode')} statt isoliert")
        pid = str(p.get("positionId", ""))
        liq = _f(p.get("liqPrice"))
        out_pos.append({
            "symbol": venue_key("bitunix", str(p.get("symbol", ""))),
            "position_id": pid,
            "qty": float(qty),
            "entry": _f(p.get("avgOpenPrice")),
            "upnl": _f(p.get("unrealizedPNL")) or 0.0,
            "realized": _f(p.get("realizedPNL")) or 0.0,
            "fee": _f(p.get("fee")) or 0.0,
            "funding": _f(p.get("funding")) or 0.0,
            "leverage": int(p.get("leverage") or 0),
            "margin": _f(p.get("margin")) or 0.0,
            "margin_mode": p.get("marginMode"),
            "liq": liq if liq is not None and liq > 0 else None,
            "tp": tp_by_pos.get(pid, {}).get("tp"),
            "sl": tp_by_pos.get(pid, {}).get("sl"),
            "opened": int(p.get("ctime") or 0),
        })  # fmt: skip
    out_orders = [
        {
            "id": str(o.get("orderId", "")),
            "symbol": venue_key("bitunix", str(o.get("symbol", ""))),
            "side": "buy" if o.get("side") == "BUY" else "sell",
            "kind": "limit" if (o.get("orderType") or o.get("type")) == "LIMIT" else "market",
            "qty": _f(o.get("qty")) or 0.0,
            "filled": _f(o.get("tradeQty")) or 0.0,
            "price": _f(o.get("price")),
            "reduce_only": bool(o.get("reduceOnly")),
            "tp": _f(o.get("tpPrice")),
            "sl": _f(o.get("slPrice")),
            "status": o.get("status"),
            "created": int(o.get("ctime") or 0),
        }
        for o in orders
    ]
    available = _f(account.get("available")) or 0.0
    frozen = _f(account.get("frozen")) or 0.0
    margin = _f(account.get("margin")) or 0.0
    upnl_iso = _f(account.get("isolationUnrealizedPNL")) or 0.0
    upnl_cross = _f(account.get("crossUnrealizedPNL")) or 0.0
    return {
        "account": {
            "available": available,
            "frozen": frozen,
            "margin": margin,
            "upnl": upnl_iso + upnl_cross,
            "equity": available + frozen + margin + upnl_iso + upnl_cross,
            "transfer": _f(account.get("transfer")),
            "bonus": _f(account.get("bonus")),
            "position_mode": position_mode or account.get("positionMode"),
        },
        "positions": out_pos,
        "orders": out_orders,
        "warnings": warnings,
    }


class LiveAccountService:
    def __init__(
        self,
        cfg: LiveConfig,
        aggs: dict[str, HeatmapAggregator],
        bridge: Bridge,
        cred: Credentials | None,
    ) -> None:
        self.cfg = cfg
        self.aggs = aggs
        self.bridge = bridge
        self.cred = cred
        self.status = "no_keys" if cred is None else "connecting"
        self.message = ""
        self.ws_connected = False
        self.state: dict[str, Any] | None = None
        self.updated_ms = 0
        self._dirty = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    # --- Ausgabe -----------------------------------------------------------------------

    def _mid(self, key: str) -> float | None:
        agg = self.aggs.get(key)
        if agg is None or agg.instrument is None or not agg.synced:
            return None
        bb, ba = agg.book.best_bid(), agg.book.best_ask()
        if bb is None or ba is None:
            return None
        return float((bb.price_ticks + ba.price_ticks) * agg.instrument.tick_size / 2)

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": "live_state",
            "exchange": "bitunix",
            "status": self.status,
            "message": self.message,
            "ws": self.ws_connected,
            "key": self.cred.masked_key if self.cred else None,
            "updated": self.updated_ms,
            "read_only": True,
            "max_leverage": self.cfg.max_leverage,
        }
        if self.state is not None:
            positions = []
            for p in self.state["positions"]:
                p = dict(p)
                mark = self._mid(p["symbol"])
                p["mark"] = mark if mark is not None else p["entry"]
                # uPnL mit dem eigenen Buch fortschreiben (Bitunix pusht nur bei Änderungen)
                if mark is not None and p["entry"]:
                    p["upnl"] = (mark - p["entry"]) * p["qty"]
                p["roe"] = p["upnl"] / p["margin"] * 100 if p["margin"] else 0.0
                positions.append(p)
            out.update({**self.state, "positions": positions})
        return out

    def _publish(self) -> None:
        self.bridge.set_shared("live", json.dumps(self.snapshot(), separators=(",", ":")))

    # --- Abgleich ----------------------------------------------------------------------

    async def refresh(self, rest: BitunixPrivateRest) -> None:
        try:
            account = await rest.account()
            mode = await rest.position_mode()
            positions = await rest.positions()
            orders = await rest.orders()
            tpsl = await rest.tpsl_orders()
        except BitunixApiError as e:
            self.status = "error"
            self.message = _explain(e)
            log.warning("bitunix account refresh failed", extra={"error": str(e)})
        else:
            self.state = build_state(account, mode, positions, orders, tpsl)
            self.updated_ms = int(time.time() * 1000)
            self.status = "ok"
            self.message = ""
        self._publish()

    async def _on_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        assert self.cred is not None
        self._ws = ws
        await ws.send_str(ws_login_message(self.cred))
        # Kanäle laut offiziellem Beispiel; tpsl kommt nach dem Login automatisch
        args = [{"ch": c} for c in ("balance", "position", "order")]
        await ws.send_str(json.dumps({"op": "subscribe", "args": args}))
        self.ws_connected = True
        self._dirty.set()  # nach (Re-)Connect sofort abgleichen

    async def _on_close(self, reason: str) -> None:
        self._ws = None
        self.ws_connected = False
        self._publish()

    async def _on_message(self, text: str, recv_ns: int) -> None:
        msg = json.loads(text)
        if msg.get("op") == "login":
            # gemessen: {"op":"login","data":{"msg":"...Token invalid","result":false,...}}
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
            if data.get("result") is not True:
                self.status = "error"
                self.message = _explain(BitunixApiError(f"WebSocket-Login: {data.get('msg', '?')}"))
                log.warning("bitunix private ws login rejected", extra={"reason": data.get("msg")})
                self._publish()
            return
        if msg.get("ch") in _PRIVATE_CHANNELS:
            self._dirty.set()

    async def run(self, stop: asyncio.Event) -> None:
        if not self.cfg.enabled:
            self.status = "off"
        self._publish()
        if self.cred is None or not self.cfg.enabled:
            log.info("bitunix live account off", extra={"status": self.status})
            return
        log.info("bitunix live account (read-only)", extra={"key": self.cred.masked_key})
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), trust_env=True
        ) as session:
            rest = BitunixPrivateRest(self.cred, self.cfg.rest_url, session)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    run_ws(
                        session, self.cfg.ws_url, name="bitunix:private", cfg=self.cfg.reconnect,
                        stop=stop, on_message=self._on_message, on_ws=self._on_ws,
                        on_close=self._on_close,
                    )
                )  # fmt: skip
                tg.create_task(ping_loop(lambda: self._ws, stop, self.cfg.ping_interval_s))
                tg.create_task(self._sync_loop(rest, stop))

    async def _sync_loop(self, rest: BitunixPrivateRest, stop: asyncio.Event) -> None:
        last_refresh = 0.0
        last_publish = 0.0
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_PUBLISH_S):
                    await self._dirty.wait()
            now = time.monotonic()
            if self._dirty.is_set() or now - last_refresh >= self.cfg.refresh_s:
                # mehrere Pushes kurz hintereinander -> ein Abgleich
                await asyncio.sleep(_DEBOUNCE_S)
                self._dirty.clear()
                await self.refresh(rest)
                last_refresh = last_publish = time.monotonic()
            elif self.state and self.state["positions"] and now - last_publish >= _PUBLISH_S:
                self._publish()  # uPnL mit neuem Mid
                last_publish = now


def _explain(e: BitunixApiError) -> str:
    """Typische Ursachen verständlich machen (ohne Key/Secret)."""
    text = str(e)
    low = text.lower()
    if "ip" in low and ("white" in low or "allow" in low or "bind" in low):
        return f"{text} – diese IP steht nicht in der Whitelist des API-Keys"
    if (
        any(w in low for w in ("token invalid", "sign", "api-key", "apikey", "key"))
        or e.code == 10003
    ):
        return f"{text} – Key/Secret prüfen (richtig kopiert? Leerzeichen?)"
    if "timestamp" in low or "time" in low:
        return f"{text} – Systemuhr stimmt nicht (max. 60 s Abweichung erlaubt)"
    return text
