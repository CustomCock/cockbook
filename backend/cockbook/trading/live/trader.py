"""Bitunix-Live – Stufe 2: Orders senden, ändern, löschen, Positionen schließen, TP/SL.

Jede Anfrage aus dem Browser läuft hier durch:
1. **Freigabe**: ``trading_enabled`` in der Config (harter Schalter, der Browser schaltet
   zusätzlich frei). Ohne Keys gar nichts.
2. **Prüfen** mit dem zuletzt abgeglichenen Kontostand: Neue/größere Positionen nur bei
   frischem Stand (``max_state_age_s``) und intaktem Abgleich. *Risiko abbauen* (Löschen,
   Schließen, Kill-Switch) geht immer – ein hängender Abgleich darf nie verhindern, dass
   man rauskommt.
3. **Umrechnen** aufs Raster der Börse (Preis auf Tick, Menge auf Schrittweite abgerundet),
   isolierte Margin sicherstellen, Hebel-Obergrenze prüfen.
4. **Senden** und ins Order-Log schreiben (``<data_dir>/live/orders.jsonl``, ohne Keys).

Danach stößt der Dienst einen Abgleich an: Bitunix schreibt selbst, dass eine erfolgreiche
Antwort beim Löschen/Ändern nicht heißt, dass es geklappt hat.

Bitunix kennt keine Stop-Einstiegsorders – live gibt es Market und Limit, TP/SL hängt an
der Order bzw. Position.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cockbook.config import LiveConfig
from cockbook.core.venue import split_key
from cockbook.trading.live.bitunix_client import BitunixApiError, BitunixTradeRest

log = logging.getLogger(__name__)

# Ändern das Risiko nicht nach oben -> auch ohne frischen Abgleich erlaubt
_RISK_REDUCING = {"cancel", "cancel_all", "close", "close_all"}


# Bitunix-Fehlercodes, die wir gesehen haben, in verständlich (Rohtext bleibt dahinter stehen)
_KNOWN_CODES = {
    10002: "Parameter von Bitunix abgelehnt",
    10003: "Key ungültig",
    30016: "Orderwert zu klein (min. 100 USD)",
}


class LiveRejectedError(Exception):
    """Anfrage wird nicht an die Börse geschickt; Text geht an den Nutzer."""


def _dec(v: Any, name: str, *, required: bool = False) -> Decimal | None:
    if v is None or v == "":
        if required:
            raise LiveRejectedError(f"{name} fehlt")
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise LiveRejectedError(f"{name}: keine Zahl") from None
    if not d.is_finite() or d <= 0:
        raise LiveRejectedError(f"{name} muss > 0 sein")
    return d


def _on_grid(v: Decimal, step: Decimal, *, down: bool = False) -> Decimal:
    q = (
        (v / step).to_integral_value(rounding=ROUND_DOWN)
        if down
        else (v / step).to_integral_value()
    )
    return (q * step).normalize()


def _s(d: Decimal) -> str:
    """Zahl als String ohne Exponent (Bitunix will "0.001", nicht "1E-3")."""
    return format(d.normalize(), "f")


class LiveTrader:
    def __init__(
        self,
        cfg: LiveConfig,
        rest: BitunixTradeRest,
        grid: Callable[[str], tuple[Decimal, Decimal] | None],
        state: Callable[[], dict[str, Any] | None],
        status: Callable[[], tuple[str, int]],
        log_path: Path | None,
    ) -> None:
        """``grid(key)`` -> (Tick, Schrittweite); ``state()`` -> letzter Abgleich;
        ``status()`` -> (Status, Zeitpunkt des letzten Abgleichs in ms)."""
        self.cfg = cfg
        self.rest = rest
        self.grid = grid
        self.state = state
        self.status = status
        self.log_path = log_path

    # --- Einstieg ----------------------------------------------------------------------

    async def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = str(msg.get("op", ""))
        req = msg.get("req")
        data: dict[str, Any] = {}
        try:
            self._guard(op)
            text, data = await self._dispatch(op, msg)
            ok = True
        except LiveRejectedError as e:
            text, ok = str(e), False
        except BitunixApiError as e:
            hint = _KNOWN_CODES.get(e.code or 0)
            text, ok = (f"Bitunix: {hint} ({e})" if hint else f"Bitunix: {e}"), False
        self._log(op, msg, ok, text)
        return {"type": "live_result", "req": req, "ok": ok, "message": text, "data": data}

    def _guard(self, op: str) -> None:
        if op == "settings":
            return  # nur lesen
        if not self.cfg.trading_enabled:
            raise LiveRejectedError(
                "Live-Trading ist aus – in config.yaml trading.live.trading_enabled: true setzen "
                "und das Backend neu starten"
            )
        if op in _RISK_REDUCING:
            return
        status, updated_ms = self.status()
        if status != "ok":
            raise LiveRejectedError("Kein gültiger Kontostand von Bitunix – erst Verbindung prüfen")
        age = time.time() - updated_ms / 1000
        if age > self.cfg.max_state_age_s:
            raise LiveRejectedError(
                f"Kontostand ist {age:.0f} s alt – zur Sicherheit keine neuen Orders "
                "(Schließen/Löschen geht weiterhin)"
            )

    # --- Aktionen ----------------------------------------------------------------------

    async def _dispatch(self, op: str, msg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = msg.get("symbol")
        sym = self._raw(key) if isinstance(key, str) else None
        if sym is None and op not in ("cancel_all", "close_all"):
            raise LiveRejectedError("Symbol fehlt")
        if op == "settings":
            assert sym is not None
            s = await self.rest.trading_settings(sym)
            if s is None:  # noch nie eingestellt: wird vor der ersten Order gesetzt
                return "", {"symbol": key, "leverage": None, "margin_mode": None}
            return "", {"symbol": key, "leverage": s.get("leverage"),
                        "margin_mode": s.get("marginMode")}  # fmt: skip
        if op == "order":
            assert isinstance(key, str)
            assert sym is not None
            return await self._order(key, sym, msg), {}
        if op == "cancel":
            assert sym is not None
            res = await self.rest.cancel_orders(sym, [str(msg.get("id"))])
            failed = res.get("failureList") or []
            if failed:
                raise LiveRejectedError(f"Löschen abgelehnt: {failed[0].get('errorMsg', '?')}")
            return "Order gelöscht", {}
        if op == "cancel_all":
            await self.rest.cancel_all_orders(sym)
            return "Alle Orders gelöscht", {}
        if op == "modify":
            assert isinstance(key, str)
            assert sym is not None
            return await self._modify(key, sym, msg), {}
        if op == "close":
            assert isinstance(key, str)
            pos = self._position(key)
            await self.rest.flash_close(pos["position_id"])
            return "Position wird geschlossen (Market)", {}
        if op == "close_all":
            # Reihenfolge: erst Orders weg (sonst könnte eine Limit-Order neu eröffnen)
            await self.rest.cancel_all_orders()
            await self.rest.close_all_positions()
            return "Kill-Switch: alle Orders gelöscht, alle Positionen werden geschlossen", {}
        if op == "tpsl":
            assert isinstance(key, str)
            assert sym is not None
            return await self._tpsl(key, sym, msg), {}
        if op == "leverage":
            assert sym is not None
            lev = msg.get("value")
            if not isinstance(lev, int) or not 1 <= lev <= self.cfg.max_leverage:
                raise LiveRejectedError(f"Hebel 1–{self.cfg.max_leverage}")
            await self.rest.change_leverage(sym, lev)
            return f"Hebel {lev}x gesetzt", {}
        raise LiveRejectedError(f"unbekannte Aktion {op!r}")

    def _raw(self, key: str) -> str:
        exchange, sym = split_key(key)
        if exchange != "bitunix":
            raise LiveRejectedError(
                "Live-Trading gibt es nur für Bitunix-Coins – oben Bitunix wählen"
            )
        return sym

    def _grid(self, key: str) -> tuple[Decimal, Decimal]:
        g = self.grid(key)
        if g is None:
            raise LiveRejectedError("Instrument unbekannt – Coin erst im Chart öffnen")
        return g

    def _position(self, key: str) -> dict[str, Any]:
        st = self.state() or {}
        pos = next((p for p in st.get("positions", []) if p["symbol"] == key), None)
        if pos is None:
            raise LiveRejectedError("Keine offene Position (laut letztem Abgleich)")
        return dict(pos)

    def _mark(self, key: str) -> Decimal | None:
        st = self.state() or {}
        pos = next((p for p in st.get("positions", []) if p["symbol"] == key), None)
        mark = pos.get("mark") if pos else None
        return Decimal(str(mark)) if mark else None

    async def _check_size(
        self, sym: str, kind: str, qty: Decimal, price: Decimal | None, reduce_only: bool
    ) -> None:
        """Grenzen der Börse vorab prüfen – klare Meldung statt Bitunix-Fehlercode."""
        info = await self.rest.pair_info(sym)
        if info is not None:
            if info.get("symbolStatus") not in (None, "OPEN"):
                raise LiveRejectedError(f"{sym} ist bei Bitunix gerade nicht handelbar "
                                        f"({info.get('symbolStatus')})")  # fmt: skip
            if info.get("isApiSupported") is False:
                raise LiveRejectedError(f"{sym} kann bei Bitunix nicht per API gehandelt werden")
            min_qty = Decimal(str(info.get("minTradeVolume") or 0))
            if not reduce_only and qty < min_qty:
                raise LiveRejectedError(
                    f"Menge zu klein: Bitunix verlangt min. {_s(min_qty)} {sym}"
                )
            limit_key = "maxLimitOrderVolume" if kind == "limit" else "maxMarketOrderVolume"
            max_qty = Decimal(str(info.get(limit_key) or 0))
            if max_qty > 0 and qty > max_qty:
                raise LiveRejectedError(
                    f"Menge zu groß: max. {_s(max_qty)} je {kind.capitalize()}-Order"
                )
        min_usd = self.cfg.min_order_usd
        if not reduce_only and price is not None and qty * price < min_usd:
            raise LiveRejectedError(
                f"Orderwert {qty * price:.2f} USDT – Bitunix verlangt mindestens "
                f"{min_usd:.0f} USDT je Order"
            )

    async def _prepare_symbol(self, key: str, sym: str, want_lev: int | None) -> int:
        """Vor einer positionserhöhenden Order: isolierte Margin und gewünschten Hebel
        sicherstellen. Gibt den Hebel zurück, mit dem die Order läuft."""
        if want_lev is not None and not 1 <= want_lev <= self.cfg.max_leverage:
            raise LiveRejectedError(f"Hebel 1–{self.cfg.max_leverage}")
        s = await self.rest.trading_settings(sym)
        st = self.state() or {}
        busy = any(p["symbol"] == key for p in st.get("positions", [])) or any(
            o["symbol"] == key for o in st.get("orders", [])
        )
        mode = s.get("marginMode") if s else None
        if mode != "ISOLATION":
            if busy and mode is not None:
                raise LiveRejectedError(
                    f"{sym} steht auf {mode} – mit offener Position/Order nicht umstellbar. "
                    "In Bitunix auf isoliert stellen."
                )
            await self.rest.change_margin_mode(sym, "ISOLATION")
            log.info("bitunix margin mode set to isolation", extra={"symbol": sym})
        lev = int(s.get("leverage") or 0) if s else 0
        if want_lev is not None and want_lev != lev:
            # Hebel aus der Ordermaske – sonst handelte man mit einem Hebel, den man nicht sieht
            await self.rest.change_leverage(sym, want_lev)
            log.info("bitunix leverage set", extra={"symbol": sym, "leverage": want_lev})
            lev = want_lev
        if lev == 0:
            raise LiveRejectedError(
                "Hebel für diesen Coin unbekannt – in der Maske einen Hebel wählen"
            )
        if lev > self.cfg.max_leverage:
            raise LiveRejectedError(
                f"Hebel bei Bitunix steht auf {lev}x, erlaubt sind max. {self.cfg.max_leverage}x"
            )
        return lev

    def _tpsl_fields(self, tp: Decimal | None, sl: Decimal | None) -> dict[str, str]:
        out: dict[str, str] = {}
        if tp is not None:
            out |= {"tpPrice": _s(tp), "tpStopType": self.cfg.tpsl_trigger, "tpOrderType": "MARKET"}
        if sl is not None:
            out |= {"slPrice": _s(sl), "slStopType": self.cfg.tpsl_trigger, "slOrderType": "MARKET"}
        return out

    async def _order(self, key: str, sym: str, msg: dict[str, Any]) -> str:
        side, kind = msg.get("side"), msg.get("kind")
        if side not in ("buy", "sell"):
            raise LiveRejectedError("ungültige Seite")
        if kind not in ("market", "limit"):
            raise LiveRejectedError("Bitunix kennt live nur Market und Limit (kein Stop-Einstieg)")
        tick, step = self._grid(key)
        qty_in = _dec(msg.get("qty"), "Menge", required=True)
        assert qty_in is not None
        qty = _on_grid(qty_in, step, down=True)
        if qty <= 0:
            raise LiveRejectedError(f"Menge zu klein (Schrittweite {_s(step)})")
        price = _dec(msg.get("price"), "Preis", required=kind == "limit")
        price = _on_grid(price, tick) if price is not None else None
        tp = _dec(msg.get("tp"), "Take-Profit")
        sl = _dec(msg.get("sl"), "Stop-Loss")
        tp = _on_grid(tp, tick) if tp is not None else None
        sl = _on_grid(sl, tick) if sl is not None else None
        ref = price or (Decimal(str(msg["ref"])) if msg.get("ref") else None)
        if ref is not None:
            long = side == "buy"
            if tp is not None and (tp <= ref if long else tp >= ref):
                raise LiveRejectedError("Take-Profit liegt auf der falschen Seite des Einstiegs")
            if sl is not None and (sl >= ref if long else sl <= ref):
                raise LiveRejectedError("Stop-Loss liegt auf der falschen Seite des Einstiegs")
        reduce_only = bool(msg.get("reduce_only"))
        await self._check_size(sym, kind, qty, price or ref, reduce_only)
        if not reduce_only:
            want = msg.get("leverage")
            await self._prepare_symbol(key, sym, want if isinstance(want, int) else None)
        body: dict[str, Any] = {
            "symbol": sym,
            "side": "BUY" if side == "buy" else "SELL",
            "orderType": "LIMIT" if kind == "limit" else "MARKET",
            "qty": _s(qty),
            "reduceOnly": reduce_only,
            "clientId": f"cb{secrets.token_hex(10)}",
        }
        if kind == "limit":
            assert price is not None
            body |= {"price": _s(price), "effect": "GTC"}
        body |= self._tpsl_fields(tp, sl)
        res = await self.rest.place_order(body)
        what = "Kauf" if side == "buy" else "Verkauf"
        at = f" @ {_s(price)}" if price is not None else ""
        oid = res.get("orderId", "?")
        return f"LIVE {kind.capitalize()}-{what} {_s(qty)}{at} gesendet (Order {oid})"

    async def _modify(self, key: str, sym: str, msg: dict[str, Any]) -> str:
        oid = str(msg.get("id"))
        st = self.state() or {}
        o = next((x for x in st.get("orders", []) if x["id"] == oid), None)
        if o is None:
            raise LiveRejectedError("Order nicht (mehr) offen")
        tick, _ = self._grid(key)
        price = _dec(msg.get("price"), "Preis", required=True)
        assert price is not None
        body: dict[str, Any] = {"orderId": oid, "symbol": sym, "qty": _s(Decimal(str(o["qty"]))),
                                "price": _s(_on_grid(price, tick))}  # fmt: skip
        # TP/SL der Order mitgeben – sonst gingen sie beim Ändern evtl. verloren
        tp = Decimal(str(o["tp"])) if o.get("tp") else None
        sl = Decimal(str(o["sl"])) if o.get("sl") else None
        body |= self._tpsl_fields(tp, sl)
        await self.rest.modify_order(body)
        return f"Order auf {body['price']} verschoben"

    async def _tpsl(self, key: str, sym: str, msg: dict[str, Any]) -> str:
        pos = self._position(key)
        tick, _ = self._grid(key)
        tp = _dec(msg.get("tp"), "Take-Profit")
        sl = _dec(msg.get("sl"), "Stop-Loss")
        tp = _on_grid(tp, tick) if tp is not None else None
        sl = _on_grid(sl, tick) if sl is not None else None
        mark = self._mark(key)
        if mark is not None:
            long = pos["qty"] > 0
            if tp is not None and (tp <= mark if long else tp >= mark):
                raise LiveRejectedError(
                    "Take-Profit würde sofort auslösen (falsche Seite des Kurses)"
                )
            if sl is not None and (sl >= mark if long else sl <= mark):
                raise LiveRejectedError(
                    "Stop-Loss würde sofort auslösen (falsche Seite des Kurses)"
                )
        ids: list[str] = pos.get("tpsl_ids") or []
        had_tp, had_sl = pos.get("tp") is not None, pos.get("sl") is not None
        removes = (had_tp and tp is None) or (had_sl and sl is None)
        body = {"symbol": sym, "positionId": pos["position_id"]} | self._tpsl_fields(tp, sl)
        if ids and not removes and (tp is not None or sl is not None):
            await self.rest.modify_position_tpsl(body)
            return "TP/SL geändert"
        # Entfernen geht nur über Löschen; danach ggf. neu setzen
        for oid in ids:
            await self.rest.cancel_tpsl(sym, oid)
        if tp is None and sl is None:
            return "TP/SL entfernt"
        await self.rest.place_position_tpsl(body)
        return "TP/SL gesetzt"

    # --- Order-Log ---------------------------------------------------------------------

    def _log(self, op: str, msg: dict[str, Any], ok: bool, text: str) -> None:
        if op == "settings":
            return
        log.info("live order request", extra={"op": op, "ok": ok, "result": text})
        if self.log_path is None:
            return
        entry = {"ts": time.time(), "op": op, "ok": ok, "result": text,
                 "request": {k: v for k, v in msg.items() if k not in ("type", "req")}}  # fmt: skip
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("live order log not writable", extra={"error": repr(e)})
