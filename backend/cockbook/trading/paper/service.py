"""Paper-Trading-Dienst: verbindet die Engine mit Marktdaten, Bridge und Festplatte.

- Marktdaten: Trades vom EventBus (Warteschlangen der Limits), Orderbuch und BBO direkt aus
  den Aggregatoren des Feeds – dieselben Bücher, die die Heatmap zeigt.
- Browser: Anfragen ``{"type": "paper", "op": ...}`` über die Bridge, Antwort
  ``paper_result`` an den Absender; der Zustand (``paper_state``) geht an alle Clients,
  höchstens alle ``publish_interval_ms`` (der PnL ändert sich mit jedem Tick).
- Festplatte: ``<data_dir>/paper/account.json`` (atomar ersetzt), damit Konto, Positionen
  und Journal Neustarts überstehen.

Später kommt daneben ein Live-Dienst mit derselben Schnittstelle (Bitunix-API); Ordermaske
und Anzeige im Frontend bleiben dieselben.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cockbook.config import PaperConfig
from cockbook.core.aggregation import HeatmapAggregator
from cockbook.core.eventbus import EventBus, Overflow
from cockbook.core.events import Side, Trade
from cockbook.core.venue import COMBINED, split_key, venue_key
from cockbook.server.bridge import Bridge
from cockbook.trading.paper.engine import (
    Account,
    OrderRejectedError,
    PaperEngine,
    account_from_json,
    account_to_json,
)

log = logging.getLogger(__name__)

_TICK_S = 0.1
_SAVE_EVERY_S = 2.0


class AggregatorMarket:
    """``MarketView`` über den Aggregator einer Börse (live Buch, ohne Gedächtnis-Level)."""

    def __init__(self, agg: HeatmapAggregator) -> None:
        assert agg.instrument is not None
        self.agg = agg
        self.exchange = agg.instrument.exchange
        self.tick_size = agg.instrument.tick_size
        self.step_size = agg.instrument.step_size

    def bbo(self) -> tuple[Decimal, Decimal] | None:
        if not self.agg.synced:
            return None
        bb, ba = self.agg.book.best_bid(), self.agg.book.best_ask()
        if bb is None or ba is None:
            return None
        return bb.price_ticks * self.tick_size, ba.price_ticks * self.tick_size

    def level_qty(self, side: Side, price: Decimal) -> Decimal:
        ticks = price / self.tick_size
        if ticks != ticks.to_integral_value():
            return Decimal(0)
        return self.agg.book.qty(side, int(ticks))

    def levels(self, side: Side) -> Iterable[tuple[Decimal, Decimal]]:
        for p, q in self.agg.book.iter_best(side):
            yield p * self.tick_size, q


def _dec(v: Any, name: str, *, required: bool = False) -> Decimal | None:
    if v is None or v == "":
        if required:
            raise OrderRejectedError(f"{name} fehlt")
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise OrderRejectedError(f"{name}: keine Zahl") from None
    if not d.is_finite() or d <= 0:
        raise OrderRejectedError(f"{name} muss > 0 sein")
    return d


class PaperService:
    def __init__(
        self,
        cfg: PaperConfig,
        aggs: dict[str, HeatmapAggregator],
        bus: EventBus,
        bridge: Bridge,
        data_dir: Path,
    ) -> None:
        self.cfg = cfg
        self.aggs = aggs
        self.bus = bus
        self.bridge = bridge
        self.path = data_dir / "paper" / "account.json"
        self._views: dict[str, AggregatorMarket] = {}
        self.engine = PaperEngine(cfg, self._market, self._load())
        self._saved_version = self.engine.version
        self._published = -1.0
        self._published_version = -1

    # --- Marktdaten --------------------------------------------------------------------

    def _market(self, symbol: str) -> AggregatorMarket | None:
        if split_key(symbol)[0] == COMBINED:
            return None  # im kombinierten Buch kann man nicht handeln
        v = self._views.get(symbol)
        if v is None:
            agg = self.aggs.get(symbol)
            if agg is None or agg.instrument is None:
                return None
            v = self._views[symbol] = AggregatorMarket(agg)
        return v

    async def _on_trade(self, e: Trade) -> None:
        key = venue_key(e.exchange, e.symbol)
        m = self._market(key)
        if m is None:
            return
        self.engine.on_trade(key, e.price_ticks * m.tick_size, e.qty, e.aggressor, e.ts_ns)

    # --- Anfragen aus dem Browser ------------------------------------------------------

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Eine Anfrage ausführen. Fehler gehen als Text an den Nutzer zurück."""
        req = msg.get("req")
        try:
            text = self._dispatch(msg)
            ok = True
        except OrderRejectedError as e:
            text, ok = str(e), False
        if not ok:
            log.info("paper order rejected", extra={"op": msg.get("op"), "reason": text})
        self._publish(force=True)
        return {"type": "paper_result", "req": req, "ok": ok, "message": text}

    def _dispatch(self, msg: dict[str, Any]) -> str:
        e = self.engine
        now = time.time_ns()
        op = msg.get("op")
        sym = msg.get("symbol")
        if op in ("order", "close", "tpsl", "leverage", "cancel_all") and sym is not None:
            if not isinstance(sym, str):
                raise OrderRejectedError("Symbol fehlt")
            if split_key(sym)[0] == COMBINED:
                raise OrderRejectedError("Im kombinierten Buch nicht handelbar – Börse wählen")
        if op == "order":
            assert isinstance(sym, str)
            side, kind = msg.get("side"), msg.get("kind")
            if side not in ("buy", "sell") or kind not in ("market", "limit", "stop"):
                raise OrderRejectedError("ungültige Order")
            qty = _dec(msg.get("qty"), "Menge", required=True)
            assert qty is not None
            o = e.place(
                sym, side, kind, qty,
                price=_dec(msg.get("price"), "Preis"),
                trigger=_dec(msg.get("trigger"), "Auslösepreis"),
                reduce_only=bool(msg.get("reduce_only")),
                tp=_dec(msg.get("tp"), "Take-Profit"),
                sl=_dec(msg.get("sl"), "Stop-Loss"),
                now_ns=now,
            )  # fmt: skip
            what = {"market": "Market", "limit": "Limit", "stop": "Stop"}[kind]
            verb = "Kauf" if side == "buy" else "Verkauf"
            if o.remaining <= 0:
                return f"{what}-{verb} {o.qty.normalize()} ausgeführt"
            return f"{what}-{verb} {o.qty.normalize()} platziert ({o.id})"
        if op == "cancel":
            e.cancel(str(msg.get("id")))
            return "Order gelöscht"
        if op == "cancel_all":
            n = e.cancel_all(sym if isinstance(sym, str) else None)
            return f"{n} Order(s) gelöscht"
        if op == "modify":
            e.modify(str(msg.get("id")), price=_dec(msg.get("price"), "Preis"),
                     trigger=_dec(msg.get("trigger"), "Auslösepreis"))  # fmt: skip
            return "Order verschoben"
        if op == "close":
            assert isinstance(sym, str)
            e.close(sym, now)
            return "Position geschlossen"
        if op == "close_all":
            errors = e.close_all(now)
            if errors:
                raise OrderRejectedError("; ".join(errors))
            return "Alles geschlossen, alle Orders gelöscht"
        if op == "tpsl":
            assert isinstance(sym, str)
            e.set_tpsl(sym, _dec(msg.get("tp"), "Take-Profit"), _dec(msg.get("sl"), "Stop-Loss"))
            return "TP/SL gesetzt"
        if op == "leverage":
            assert isinstance(sym, str)
            lev = msg.get("value")
            if not isinstance(lev, int):
                raise OrderRejectedError("Hebel muss eine ganze Zahl sein")
            e.set_leverage(sym, lev)
            return f"Hebel {lev}x"
        if op == "reset":
            if msg.get("confirm") is not True:
                raise OrderRejectedError("Zurücksetzen braucht eine Bestätigung")
            e.reset()
            return "Paper-Konto zurückgesetzt"
        if op == "state":
            return ""
        raise OrderRejectedError(f"unbekannte Aktion {op!r}")

    # --- Ausgabe -----------------------------------------------------------------------

    def _publish(self, *, force: bool = False) -> None:
        e = self.engine
        now = time.monotonic()
        live = bool(e.acc.positions)  # PnL bewegt sich nur mit offenen Positionen
        due = now - self._published >= self.cfg.publish_interval_ms / 1000
        if not force and not (due and (live or e.version != self._published_version)):
            return
        self._published = now
        self._published_version = e.version
        self.bridge.set_shared("paper", json.dumps(e.snapshot(), separators=(",", ":")))
        for level, text in e.messages:
            self.bridge.broadcast(json.dumps({"type": "paper_event", "level": level,
                                              "message": text}))  # fmt: skip
        e.messages.clear()

    # --- Festplatte --------------------------------------------------------------------

    def _load(self) -> Account | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.error("paper account unreadable, starting fresh", extra={"error": repr(e)})
            return None
        try:
            acc = account_from_json(raw)
        except (KeyError, TypeError, ValueError, InvalidOperation) as e:
            log.error("paper account invalid, starting fresh", extra={"error": repr(e)})
            return None
        log.info("paper account loaded", extra={"positions": len(acc.positions)})
        return acc

    def _serialize(self) -> str:
        """Im Loop-Thread serialisieren: die Engine ändert sich nur dort."""
        self._saved_version = self.engine.version
        return json.dumps(account_to_json(self.engine.acc))

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)  # atomar: nie eine halb geschriebene Datei

    # --- Lauf --------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        self.bus.subscribe(Trade, self._on_trade, name="paper:Trade", overflow=Overflow.BLOCK)
        last_save = time.monotonic()
        self._publish(force=True)
        try:
            while not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(_TICK_S):
                        await stop.wait()
                self.engine.on_tick(time.time_ns())
                self._publish()
                if (self.engine.version != self._saved_version
                        and time.monotonic() - last_save >= _SAVE_EVERY_S):  # fmt: skip
                    await asyncio.to_thread(self._write, self._serialize())
                    last_save = time.monotonic()
        finally:
            if self.engine.version != self._saved_version:
                self._write(self._serialize())
