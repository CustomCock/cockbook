"""Börsenunabhängige Verbindungs-Bausteine für Adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import aiohttp

from cockbook.config import ReconnectConfig

log = logging.getLogger(__name__)


class MarketDataSource(Protocol):
    """Gemeinsames Interface für Live-Adapter und (ab Phase 4) Replay.

    ``run`` publiziert normalisierte Events auf den EventBus, bis ``stop`` gerufen wird.
    """

    name: str

    async def run(self) -> None: ...

    def stop(self) -> None: ...


class Backoff:
    """Exponentieller Backoff mit "Full Jitter".

    Jitter verhindert, dass viele Clients nach einem Börsen-Ausfall im Gleichschritt
    reconnecten (Thundering Herd) – bei mehreren Symbolen/Verbindungen relevant.
    """

    def __init__(self, initial: float, maximum: float) -> None:
        self.initial = initial
        self.maximum = maximum
        self._attempt = 0

    def next(self) -> float:
        cap = min(self.maximum, self.initial * 2**self._attempt)
        self._attempt += 1
        return random.uniform(cap / 2, cap)

    def reset(self) -> None:
        self._attempt = 0


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Schläft ``seconds`` oder bis ``stop`` gesetzt wird. True = Stop angefordert."""
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
        return True
    except TimeoutError:
        return False


class _StaleConnectionError(Exception):
    pass


async def run_ws(
    session: aiohttp.ClientSession,
    url: str,
    *,
    name: str,
    cfg: ReconnectConfig,
    stop: asyncio.Event,
    on_message: Callable[[str, int], Awaitable[None]],
    on_open: Callable[[], Awaitable[None]] | None = None,
    on_close: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Hält eine WebSocket-Verbindung am Leben, bis ``stop`` gesetzt ist.

    - Ping/Pong: aiohttp beantwortet Server-Pings automatisch (``autoping``). Binance
      pingt alle 3 min und trennt ohne Pong nach 10 min.
    - Heartbeat: kommt ``stale_timeout_s`` lang *gar nichts*, gilt die Verbindung als
      tot (halb-offene TCP-Verbindungen merkt man sonst erst sehr spät).
    - Fehler in ``on_message`` (z. B. unerwartetes Nachrichtenformat) werden geloggt und
      führen zu Reconnect + Neu-Sync statt zu einem toten Task.
    """
    backoff = Backoff(cfg.initial_backoff_s, cfg.max_backoff_s)
    while not stop.is_set():
        connected_at: float | None = None
        reason = "unknown"
        try:
            async with session.ws_connect(url, autoping=True, heartbeat=None) as ws:
                connected_at = time.monotonic()
                log.info("ws connected", extra={"conn": name, "url": url})
                if on_open is not None:
                    await on_open()
                stop_task = asyncio.ensure_future(stop.wait())
                try:
                    while True:
                        recv = asyncio.ensure_future(ws.receive())
                        done, _ = await asyncio.wait(
                            {recv, stop_task},
                            timeout=cfg.stale_timeout_s,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done:
                            recv.cancel()
                            reason = "stopped"
                            break
                        if not done:
                            recv.cancel()
                            raise _StaleConnectionError
                        msg = recv.result()
                        if msg.type is aiohttp.WSMsgType.TEXT:
                            await on_message(msg.data, time.time_ns())
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            reason = f"closed by server (code={ws.close_code})"
                            break
                        elif msg.type is aiohttp.WSMsgType.ERROR:
                            reason = f"ws error: {ws.exception()!r}"
                            break
                finally:
                    stop_task.cancel()
        except _StaleConnectionError:
            reason = f"no data for {cfg.stale_timeout_s}s"
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            reason = f"{type(e).__name__}: {e}"
        except asyncio.CancelledError:
            if on_close is not None and connected_at is not None:
                await on_close("cancelled")
            raise
        except Exception as e:
            reason = f"handler error: {type(e).__name__}: {e}"
            log.exception("ws message handler failed", extra={"conn": name})

        if connected_at is not None:
            if on_close is not None:
                await on_close(reason)
            if time.monotonic() - connected_at >= cfg.stable_after_s:
                backoff.reset()
        if stop.is_set():
            break
        delay = backoff.next()
        log.warning(
            "ws disconnected, reconnecting",
            extra={"conn": name, "reason": reason, "retry_in_s": round(delay, 2)},
        )
        if await sleep_or_stop(stop, delay):
            break
    log.info("ws loop stopped", extra={"conn": name})


class RestError(Exception):
    pass


async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: Mapping[str, str | int] | None = None,
    *,
    attempts: int = 5,
    backoff: Backoff | None = None,
) -> tuple[Any, str]:
    """GET mit Retry. Gibt (geparstes JSON, Rohtext) zurück.

    429 (Rate-Limit) und 418 (IP-Ban) werden respektiert: ``Retry-After`` abwarten,
    niemals hämmern – wiederholte 429 führen bei Binance zum automatischen IP-Ban.
    """
    backoff = backoff or Backoff(1.0, 30.0)
    last_err = ""
    for _ in range(attempts):
        try:
            async with session.get(url, params=params) as resp:
                text = await resp.text()
                if resp.status == 200:
                    return json.loads(text), text
                last_err = f"HTTP {resp.status}: {text[:200]}"
                if resp.status in (418, 429):
                    retry_after = float(resp.headers.get("Retry-After", "60"))
                    log.error(
                        "rate limited by exchange, backing off",
                        extra={"url": url, "status": resp.status, "retry_after_s": retry_after},
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status < 500:
                    raise RestError(last_err)
        except (aiohttp.ClientError, TimeoutError) as e:
            last_err = f"{type(e).__name__}: {e}"
        delay = backoff.next()
        log.warning("rest request failed, retrying", extra={"url": url, "error": last_err})
        await asyncio.sleep(delay)
    raise RestError(f"{url}: {last_err}")
