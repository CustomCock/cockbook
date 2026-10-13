"""End-to-End: Adapter gegen Fake-Server – Sync, Lücke, Reconnect, Trades, Rohdaten."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

from cockbook.adapters.binance import BinanceUsdmAdapter
from cockbook.config import BinanceUsdmConfig, BookValidationConfig, ReconnectConfig
from cockbook.core.eventbus import EventBus
from cockbook.core.events import (
    BookDelta,
    BookSnapshot,
    BookSyncStatus,
    Event,
    InstrumentInfo,
    Liquidation,
    RawMessage,
    Side,
    Trade,
)
from cockbook.core.orderbook import OrderBook
from tests.fake_binance import FakeBinance, depth_msg, snapshot

FIX = Path(__file__).parent / "fixtures" / "binance"
B0 = (("65000.0", "1.000"), ("64999.0", "2.000"))
A0 = (("65001.0", "1.500"),)


def cfg_for(fake: FakeBinance) -> BinanceUsdmConfig:
    return BinanceUsdmConfig(
        name="binance_usdm",
        rest_url=f"http://{fake.base}",
        ws_public_url=f"ws://{fake.base}/public",
        ws_market_url=f"ws://{fake.base}/market",
        validation=BookValidationConfig(enabled=False),
        reconnect=ReconnectConfig(initial_backoff_s=0.05, max_backoff_s=0.1, stale_timeout_s=5),
    )


async def wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


def book_trace(events: list[Event]) -> list[str]:
    out = []
    for e in events:
        if isinstance(e, BookSnapshot):
            out.append(f"S{e.last_seq}")
        elif isinstance(e, BookDelta):
            out.append(f"D{e.last_seq}")
        elif isinstance(e, BookSyncStatus):
            out.append("sync" if e.synced else "LOST")
    return out


async def test_sync_gap_reconnect_end_to_end() -> None:
    async with FakeBinance() as fake:
        fake.public_scripts = [
            [
                depth_msg(91, 95, 90),
                depth_msg(96, 105, 95, bids=[("65000.0", "3.000")]),
                depth_msg(106, 110, 105, asks=[("65001.0", "0"), ("65002.0", "0.500")]),
                ("wait_depth_calls", 1),
                depth_msg(150, 210, 999),  # Lücke: pu passt nicht
                ("wait_depth_calls", 2),
                depth_msg(211, 220, 210, bids=[("64998.0", "4.000")]),
                ("close",),
            ],
            [
                depth_msg(300, 310, 290),
                depth_msg(311, 320, 310, bids=[("65000.5", "0.100")]),
            ],
        ]
        fake.market_scripts = [
            [
                json.loads((FIX / "agg_trade_sell.json").read_text()),
                json.loads((FIX / "force_order.json").read_text()),
            ]
        ]
        for lid in (100, 200, 305):
            fake.snapshots.put_nowait(snapshot(lid, B0, A0))

        events: list[Event] = []
        downstream = OrderBook()

        async def collect(e: Event) -> None:
            events.append(e)
            if isinstance(e, BookSnapshot | BookDelta):
                downstream.apply_event(e)

        async with EventBus() as bus:
            bus.subscribe(Event, collect)
            adapter = BinanceUsdmAdapter(cfg_for(fake), "btcusdt", bus)
            task = asyncio.create_task(adapter.run())
            await wait_for(lambda: "D320" in book_trace(events))
            await wait_for(lambda: any(isinstance(e, Liquidation) for e in events))

            adapter.stop()
            async with asyncio.timeout(5):
                await task  # sauberes Herunterfahren, keine hängenden Tasks

        # Exakte Reihenfolge: Sync -> Lücke -> Neu-Sync -> Disconnect -> Neu-Sync
        assert book_trace(events) == [
            "S100", "sync", "D105", "D110",
            "LOST", "S200", "sync", "D210", "D220",
            "LOST", "S305", "sync", "D310", "D320",
            "LOST",  # Herunterfahren: Buch ist danach nicht mehr aktuell
        ]  # fmt: skip
        assert fake.depth_calls == 3
        assert fake.public_connections == 2

        # InstrumentInfo kommt vor allen Marktdaten
        first_market = next(i for i, e in enumerate(events) if not isinstance(e, RawMessage))
        assert isinstance(events[first_market], InstrumentInfo)

        # Downstream-Buch aus Bus-Events == erwarteter Zustand
        assert downstream.last_seq == 320
        bb = downstream.best_bid()
        assert bb is not None
        assert bb.price_ticks == 650005  # 65000.5 bei Tick 0.10

        trade = next(e for e in events if isinstance(e, Trade))
        assert trade.aggressor is Side.ASK
        liq = next(e for e in events if isinstance(e, Liquidation))
        assert liq.side is Side.ASK

        raw_streams = [e.stream for e in events if isinstance(e, RawMessage)]
        assert raw_streams.count("rest:depth") == 3
        assert raw_streams.count("depth") == 7
        assert "rest:exchangeInfo" in raw_streams
        metas = [
            json.loads(e.payload)
            for e in events
            if isinstance(e, RawMessage) and e.stream == "meta"
        ]
        assert any(m["event"] == "disconnect" and m["conn"] == "public" for m in metas)


async def test_stale_snapshot_is_refetched() -> None:
    async with FakeBinance() as fake:
        fake.public_scripts = [[depth_msg(91, 95, 90), depth_msg(96, 105, 95)]]
        fake.snapshots.put_nowait(snapshot(50, B0, A0))  # älter als U=91 -> unbrauchbar
        fake.snapshots.put_nowait(snapshot(100, B0, A0))
        events: list[Event] = []

        async def collect(e: Event) -> None:
            events.append(e)

        async with EventBus() as bus:
            bus.subscribe(Event, collect)
            adapter = BinanceUsdmAdapter(cfg_for(fake), "BTCUSDT", bus)
            task = asyncio.create_task(adapter.run())
            await wait_for(lambda: "D105" in book_trace(events))
            adapter.stop()
            await task
        assert book_trace(events) == ["S100", "sync", "D105", "LOST"]
        assert fake.depth_calls == 2


async def test_worker_recovers_from_malformed_snapshot(monkeypatch: object) -> None:
    import cockbook.adapters.binance as binance

    async def no_wait(stop: asyncio.Event, seconds: float) -> bool:
        await asyncio.sleep(0)
        return stop.is_set()

    monkeypatch.setattr(binance, "sleep_or_stop", no_wait)  # type: ignore[attr-defined]
    async with FakeBinance() as fake:
        fake.public_scripts = [[depth_msg(91, 95, 90), depth_msg(96, 105, 95)]]
        fake.snapshots.put_nowait(snapshot(100, (("65000.05", "1"),), A0))  # nicht auf Tick 0.10
        fake.snapshots.put_nowait(snapshot(100, B0, A0))
        events: list[Event] = []

        async def collect(e: Event) -> None:
            events.append(e)

        async with EventBus() as bus:
            bus.subscribe(Event, collect)
            adapter = BinanceUsdmAdapter(cfg_for(fake), "BTCUSDT", bus)
            task = asyncio.create_task(adapter.run())
            await wait_for(lambda: "D105" in book_trace(events))
            adapter.stop()
            await task
        assert fake.depth_calls == 2
