"""Bitunix-Futures: signierte REST-Aufrufe und Login für den privaten WebSocket.

Doku (gelesen am 2026-10-17):
- Einführung/Header: https://www.bitunix.com/api-docs/futures/common/introduction.html
- Signatur: https://www.bitunix.com/api-docs/futures/common/sign.html
- Konto: https://www.bitunix.com/api-docs/futures/account/get_single_account.html
- Positionen: https://www.bitunix.com/api-docs/futures/position/get_pending_positions.html
- Orders: https://www.bitunix.com/api-docs/futures/trade/get_pending_orders.html
- TP/SL: https://www.bitunix.com/api-docs/futures/tp_sl/get_pending_tp_sl_order.html
- Positionsmodus: https://www.bitunix.com/api-docs/futures/account/get_position_mode.html
- Privater WebSocket: https://www.bitunix.com/api-docs/futures/websocket/prepare/WebSocket.html
- Offizielles Beispiel: https://github.com/BitunixOfficial/open-api (Demo/Python/*_sign.py)

Signatur (REST): ``digest = SHA256(nonce + timestamp_ms + apiKey + queryParams + body)``,
``sign = SHA256(digest + secret)``. ``queryParams`` = Schlüssel aufsteigend sortiert,
Schlüssel und Wert direkt aneinander (``marginCoinUSDTsymbolBTCUSDT``); ``body`` = JSON ohne
Leerzeichen, exakt so gesendet, wie signiert. Zeitstempel max. 60 s neben der Serverzeit.
WebSocket-Login: gleiches Verfahren mit Zeitstempel in *Sekunden* und ohne Parameter.

Sicherheit: Key und Secret kommen nur aus der Umgebung, stehen in keinem ``repr``, keinem
Log und keiner Fehlermeldung. Gesendet wird das Secret nie – nur die Signatur.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import aiohttp

ENV_KEY = "BITUNIX_API_KEY"
ENV_SECRET = "BITUNIX_API_SECRET"  # pragma: allowlist secret (Name der Variable)


class BitunixApiError(Exception):
    """Fehler von Bitunix (``code != 0``) oder HTTP/Netz. Enthält nie Key/Secret."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    secret: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Credentials | None:
        e = os.environ if env is None else env
        key, secret = e.get(ENV_KEY, "").strip(), e.get(ENV_SECRET, "").strip()
        return cls(key, secret) if key and secret else None

    @property
    def masked_key(self) -> str:
        """Für die Anzeige: nur die letzten 4 Zeichen."""
        return f"…{self.api_key[-4:]}" if len(self.api_key) > 8 else "…"

    def __repr__(self) -> str:
        return f"Credentials(api_key={self.masked_key})"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sort_params(params: Mapping[str, Any]) -> str:
    return "".join(f"{k}{params[k]}" for k in sorted(params))


def sign_rest(cred: Credentials, nonce: str, ts_ms: str, query: str, body: str) -> str:
    return _sha256(_sha256(nonce + ts_ms + cred.api_key + query + body) + cred.secret)


def sign_ws(cred: Credentials, nonce: str, ts_s: str) -> str:
    return _sha256(_sha256(nonce + ts_s + cred.api_key) + cred.secret)


def new_nonce() -> str:
    return secrets.token_hex(16)  # 32 Zeichen wie im offiziellen Beispiel


def ws_login_message(cred: Credentials, now_s: int | None = None) -> str:
    ts = str(int(time.time()) if now_s is None else now_s)
    nonce = new_nonce()
    args = {"apiKey": cred.api_key, "timestamp": int(ts), "nonce": nonce,
            "sign": sign_ws(cred, nonce, ts)}  # fmt: skip
    return json.dumps({"op": "login", "args": [args]})


def compact_json(obj: Any) -> str:
    """JSON ohne Leerzeichen – genau dieser Text wird signiert und gesendet."""
    return json.dumps(obj, separators=(",", ":"))


class BitunixPrivateRest:
    def __init__(self, cred: Credentials, rest_url: str, session: aiohttp.ClientSession) -> None:
        self.cred = cred
        self.rest_url = rest_url.rstrip("/")
        self.session = session

    def _headers(self, query: str, body: str) -> dict[str, str]:
        nonce = new_nonce()
        ts = str(int(time.time() * 1000))
        return {
            "api-key": self.cred.api_key,
            "nonce": nonce,
            "timestamp": ts,
            "sign": sign_rest(self.cred, nonce, ts, query, body),
            "language": "en-US",
            "Content-Type": "application/json",
        }

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = self._headers(sort_params(params), "")
        url = f"{self.rest_url}{path}"
        try:
            async with self.session.get(url, params={k: str(v) for k, v in params.items()},
                                        headers=headers) as r:  # fmt: skip
                text = await r.text()
                status = r.status
        except (aiohttp.ClientError, TimeoutError) as e:
            raise BitunixApiError(f"{path}: Netzwerkfehler {type(e).__name__}") from None
        return _parse(path, status, text)

    async def post(self, path: str, body: Mapping[str, Any]) -> Any:
        """Signierter POST (für Stufe 2: Orders). Der signierte Text wird 1:1 gesendet."""
        raw = compact_json(body)
        headers = self._headers("", raw)
        url = f"{self.rest_url}{path}"
        try:
            async with self.session.post(url, data=raw.encode("utf-8"), headers=headers) as r:
                text = await r.text()
                status = r.status
        except (aiohttp.ClientError, TimeoutError) as e:
            raise BitunixApiError(f"{path}: Netzwerkfehler {type(e).__name__}") from None
        return _parse(path, status, text)

    # --- Lesen (Stufe 1) ---------------------------------------------------------------

    async def account(self) -> dict[str, Any]:
        data = await self.get("/api/v1/futures/account", {"marginCoin": "USDT"})
        rows = data if isinstance(data, list) else [data]
        return next((r for r in rows if r.get("marginCoin") == "USDT"), rows[0] if rows else {})

    async def position_mode(self) -> str:
        data = await self.get("/api/v1/futures/account/position_mode")
        return str(data.get("positionMode", ""))

    async def positions(self) -> list[dict[str, Any]]:
        data = await self.get("/api/v1/futures/position/get_pending_positions")
        return list(data or [])

    async def orders(self) -> list[dict[str, Any]]:
        """Alle offenen Orders (seitenweise, max. 100 je Abfrage)."""
        out: list[dict[str, Any]] = []
        while True:
            data = await self.get("/api/v1/futures/trade/get_pending_orders",
                                  {"limit": 100, "skip": len(out)})  # fmt: skip
            page = list((data or {}).get("orderList") or [])
            out.extend(page)
            if len(page) < 100 or len(out) >= int((data or {}).get("total") or 0):
                return out

    async def tpsl_orders(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            data = await self.get("/api/v1/futures/tpsl/get_pending_orders",
                                  {"limit": 100, "skip": len(out)})  # fmt: skip
            page = list(data or [])
            out.extend(page)
            if len(page) < 100:
                return out


class BitunixTradeRest(BitunixPrivateRest):
    """Schreibende Aufrufe (Stufe 2). Doku je Methode; alle Zahlen als Strings, wie verlangt.

    Achtung laut Doku: Eine erfolgreiche Antwort beim Löschen/Ändern heißt nicht, dass es
    geklappt hat – maßgeblich ist der WebSocket-Push. Der Dienst gleicht danach ab.
    """

    async def leverage_margin_mode(self, symbol: str) -> dict[str, Any]:
        # https://www.bitunix.com/api-docs/futures/account/get_leverage_and_margin_mode.html
        data = await self.get("/api/v1/futures/account/get_leverage_margin_mode",
                              {"symbol": symbol, "marginCoin": "USDT"})  # fmt: skip
        return dict(data or {})

    async def trading_settings(self, symbol: str) -> dict[str, Any] | None:
        """Hebel/Margin-Modus des Coins; ``None`` = für diesen Coin noch nie eingestellt.

        Erst der neuere Endpunkt (liefert nur *konfigurierte* Coins), sonst der ältere.
        Der ältere antwortet für nie eingestellte Coins mit ``Parameter error`` (10002)
        statt mit Standardwerten – das ist kein Fehler, sondern "unbekannt".
        https://www.bitunix.com/api-docs/futures/account/get_trading_settings.html
        """
        try:
            rows = await self.get("/api/v1/futures/account/trading_settings", {"symbols": symbol})
        except BitunixApiError:
            try:
                data = await self.leverage_margin_mode(symbol)
            except BitunixApiError as e:
                if e.code == 10002:
                    return None
                raise
            return data or None
        for r in rows or []:
            if str(r.get("symbol", "")).upper() == symbol.upper():
                return dict(r)
        return None

    async def change_margin_mode(self, symbol: str, mode: str) -> None:
        # https://www.bitunix.com/api-docs/futures/account/change_margin_mode.html
        # (nur ohne offene Position und Orders möglich)
        await self.post("/api/v1/futures/account/change_margin_mode",
                        {"marginMode": mode, "symbol": symbol, "marginCoin": "USDT"})  # fmt: skip

    async def change_leverage(self, symbol: str, leverage: int) -> None:
        # https://www.bitunix.com/api-docs/futures/account/change_leverage.html
        await self.post("/api/v1/futures/account/change_leverage",
                        {"symbol": symbol, "leverage": leverage, "marginCoin": "USDT"})  # fmt: skip

    async def place_order(self, body: Mapping[str, Any]) -> dict[str, Any]:
        # https://www.bitunix.com/api-docs/futures/trade/place_order.html
        return dict(await self.post("/api/v1/futures/trade/place_order", body) or {})

    async def modify_order(self, body: Mapping[str, Any]) -> dict[str, Any]:
        # https://www.bitunix.com/api-docs/futures/trade/modify_order.html
        return dict(await self.post("/api/v1/futures/trade/modify_order", body) or {})

    async def cancel_orders(self, symbol: str, order_ids: list[str]) -> dict[str, Any]:
        # https://www.bitunix.com/api-docs/futures/trade/cancel_orders.html
        body = {"symbol": symbol, "orderList": [{"orderId": i} for i in order_ids]}
        return dict(await self.post("/api/v1/futures/trade/cancel_orders", body) or {})

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        # https://www.bitunix.com/api-docs/futures/trade/cancel_all_orders.html
        await self.post("/api/v1/futures/trade/cancel_all_orders",
                        {"symbol": symbol} if symbol else {})  # fmt: skip

    async def flash_close(self, position_id: str) -> None:
        # https://www.bitunix.com/api-docs/futures/trade/flash_close_position.html
        await self.post("/api/v1/futures/trade/flash_close_position", {"positionId": position_id})

    async def close_all_positions(self) -> None:
        # https://www.bitunix.com/api-docs/futures/trade/close_all_position.html (1/s)
        await self.post("/api/v1/futures/trade/close_all_position", {})

    async def place_position_tpsl(self, body: Mapping[str, Any]) -> None:
        # https://www.bitunix.com/api-docs/futures/tp_sl/place_position_tp_sl_order.html
        await self.post("/api/v1/futures/tpsl/position/place_order", body)

    async def modify_position_tpsl(self, body: Mapping[str, Any]) -> None:
        # https://www.bitunix.com/api-docs/futures/tp_sl/modify_position_tp_sl_order.html
        await self.post("/api/v1/futures/tpsl/position/modify_order", body)

    async def cancel_tpsl(self, symbol: str, order_id: str) -> None:
        # https://www.bitunix.com/api-docs/futures/tp_sl/cancel_tp_sl_order.html
        await self.post(
            "/api/v1/futures/tpsl/cancel_order", {"symbol": symbol, "orderId": order_id}
        )


def _parse(path: str, status: int, text: str) -> Any:
    try:
        payload = json.loads(text)
    except ValueError:
        raise BitunixApiError(f"{path}: HTTP {status}, keine JSON-Antwort") from None
    code = payload.get("code") if isinstance(payload, dict) else None
    if status != 200 or code != 0:
        msg = payload.get("msg", "") if isinstance(payload, dict) else ""
        raise BitunixApiError(f"{path}: {msg or f'HTTP {status}'} (code {code})", code)
    return payload.get("data")


def dec(v: Any) -> Decimal | None:
    """Bitunix liefert Zahlen als Strings; leer/fehlend -> None."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except ArithmeticError:
        return None
