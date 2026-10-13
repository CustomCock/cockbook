"""``python -m cockbook.debug_book [--symbol BTCUSDT] [--levels 10]``

Zeigt Top-N Bid/Ask live im Terminal. Das Buch wird – wie bei jedem späteren
Consumer – ausschließlich aus den Bus-Events aufgebaut, nicht aus dem Adapter-
internen Buch. Damit testet die Ansicht auch die Event-Kette Adapter -> Bus -> Buch.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

from cockbook.app import build_sources, install_stop_signals, stop_sources_on
from cockbook.config import AppConfig, load_config
from cockbook.core.eventbus import EventBus
from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    InstrumentInfo,
    Liquidation,
    Side,
    Trade,
)
from cockbook.core.orderbook import OrderBook
from cockbook.log import ConsoleFormatter, setup_logging

CLEAR = "\x1b[H\x1b[2J"
GREEN, RED, DIM, BOLD, RESET = "\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[1m", "\x1b[0m"


class _RingHandler(logging.Handler):
    """Hält die letzten Log-Zeilen, damit sie im Bildschirm unten erscheinen statt
    die Tabelle zu zerreißen."""

    def __init__(self, n: int = 6) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=n)
        self.setFormatter(ConsoleFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record).splitlines()[0][:160])


class DebugView:
    def __init__(self, symbol: str, levels: int) -> None:
        self.symbol = symbol
        self.levels = levels
        self.book = OrderBook()
        self.tick: Decimal | None = None
        self.synced = False
        self.sync_reason = "waiting"
        self.resyncs = 0
        self.deltas = 0
        self.trades: deque[Trade] = deque(maxlen=8)
        self.liqs: deque[Liquidation] = deque(maxlen=3)
        self.last_delta_lag_ms = 0.0

    async def on_info(self, e: InstrumentInfo) -> None:
        self.tick = e.tick_size

    async def on_book(self, e: BookSnapshot | BookDelta) -> None:
        self.book.apply_event(e)
        if isinstance(e, BookDelta):
            self.deltas += 1
            if e.ts_exchange_ns:
                self.last_delta_lag_ms = (e.ts_recv_ns - e.ts_exchange_ns) / 1e6

    async def on_status(self, e: BookSyncStatus) -> None:
        if self.synced and not e.synced:
            self.resyncs += 1
        self.synced, self.sync_reason = e.synced, e.reason

    async def on_trade(self, e: Trade) -> None:
        self.trades.appendleft(e)

    async def on_liq(self, e: Liquidation) -> None:
        self.liqs.appendleft(e)

    def _px(self, ticks: int) -> str:
        # Decimal behält den Exponenten der Tick-Größe: 650001 * Decimal("0.10") = 65000.10
        return str(ticks * self.tick) if self.tick is not None else str(ticks)

    def render(self, logs: deque[str]) -> str:
        out = [CLEAR]
        state = f"{GREEN}SYNCED{RESET}" if self.synced else f"{RED}NOT SYNCED{RESET}"
        out.append(
            f"{BOLD}{self.symbol}{RESET}  {state} ({self.sync_reason})  "
            f"last_id={self.book.last_seq}  deltas={self.deltas}  resyncs={self.resyncs}  "
            f"lag={self.last_delta_lag_ms:.0f}ms  depth={self.book.depth}\n"
        )
        bids, asks = self.book.top(self.levels)
        out.append(f"{DIM}{'price':>14} {'qty':>14}{RESET}\n")
        for lvl in reversed(asks):
            out.append(f"{RED}{self._px(lvl.price_ticks):>14} {lvl.qty!s:>14}{RESET}\n")
        if bids and asks:
            spread = asks[0].price_ticks - bids[0].price_ticks
            out.append(f"{DIM}{'-- spread ' + str(spread) + ' ticks --':^29}{RESET}\n")
        for lvl in bids:
            out.append(f"{GREEN}{self._px(lvl.price_ticks):>14} {lvl.qty!s:>14}{RESET}\n")
        out.append(f"\n{BOLD}Trades{RESET}\n")
        for t in self.trades:
            c = GREEN if t.aggressor is Side.BID else RED
            out.append(f"{c}{self._px(t.price_ticks):>14} {t.qty!s:>14}{RESET}\n")
        if self.liqs:
            out.append(f"\n{BOLD}Liquidations{RESET}\n")
            for li in self.liqs:
                what = "LONG liq" if li.side is Side.ASK else "SHORT liq"
                out.append(f"{what:>10} {self._px(li.price_ticks):>14} {li.qty!s:>10}\n")
        out.append(f"\n{DIM}" + "\n".join(logs) + f"{RESET}\n")
        return "".join(out)


async def run(cfg: AppConfig, symbol: str | None, levels: int, interval: float) -> None:
    stop = asyncio.Event()
    install_stop_signals(stop)
    ring = _RingHandler()
    logging.getLogger().handlers[:] = [ring]
    async with EventBus() as bus:
        sources = build_sources(cfg, bus, only_symbol=symbol)[:1]
        view = DebugView(sources[0].symbol, levels)
        bus.subscribe(InstrumentInfo, view.on_info)
        bus.subscribe(BookSnapshot, view.on_book)
        bus.subscribe(BookDelta, view.on_book)
        bus.subscribe(BookSyncStatus, view.on_status)
        bus.subscribe(Trade, view.on_trade)
        bus.subscribe(Liquidation, view.on_liq)

        async def draw() -> None:
            while not stop.is_set():
                sys.stdout.write(view.render(ring.lines))
                sys.stdout.flush()
                await asyncio.sleep(interval)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(sources[0].run())
            tg.create_task(stop_sources_on(stop, sources))
            tg.create_task(draw())


def main() -> None:
    parser = argparse.ArgumentParser(prog="cockbook.debug_book")
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get("COCKBOOK_CONFIG", "config.yaml"))
    )
    parser.add_argument("--symbol", default=None, help="Default: erstes Symbol der Config")
    parser.add_argument("--levels", type=int, default=10)
    parser.add_argument("--interval", type=float, default=0.5, help="Refresh in Sekunden")
    args = parser.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    if os.name == "nt":
        os.system("")  # aktiviert ANSI-Escape-Codes in der Windows-Konsole
    started = time.monotonic()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(cfg, args.symbol, args.levels, args.interval))
    print(f"\nbeendet nach {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
