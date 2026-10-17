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
