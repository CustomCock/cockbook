import asyncio
from decimal import Decimal

import pytest

from cockbook.core.eventbus import EventBus, Overflow
from cockbook.core.events import Event, Liquidation, Side, Trade


def trade(i: int) -> Trade:
    return Trade(
        exchange="test",
        symbol="BTCUSDT",
        ts_recv_ns=i,
        price_ticks=100 + i,
        qty=Decimal(1),
        aggressor=Side.BID,
        trade_id=str(i),
    )


def liq(i: int) -> Liquidation:
    return Liquidation(
        exchange="test",
        symbol="BTCUSDT",
        ts_recv_ns=i,
        price_ticks=1,
        price=Decimal(1),
        qty=Decimal(1),
        side=Side.ASK,
    )


async def test_multiple_subscribers_receive_in_order() -> None:
    a: list[int] = []
    b: list[int] = []

    async def ha(e: Trade) -> None:
        a.append(e.ts_recv_ns)

    async def hb(e: Trade) -> None:
        b.append(e.ts_recv_ns)

    async with EventBus() as bus:
        bus.subscribe(Trade, ha)
        bus.subscribe(Trade, hb)
        for i in range(50):
            await bus.publish(trade(i))
    # close() drained die Queues
    assert a == list(range(50))
    assert b == list(range(50))


async def test_routing_by_type_and_base_class() -> None:
    trades: list[Event] = []
    everything: list[Event] = []

    async def on_trade(e: Trade) -> None:
        trades.append(e)

    async def on_any(e: Event) -> None:
        everything.append(e)

    async with EventBus() as bus:
        bus.subscribe(Trade, on_trade)
        bus.subscribe(Event, on_any)
        await bus.publish(trade(1))
        await bus.publish(liq(2))
    assert [type(e) for e in trades] == [Trade]
    assert [type(e) for e in everything] == [Trade, Liquidation]


async def test_handler_exception_does_not_kill_subscriber(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[int] = []

    async def flaky(e: Trade) -> None:
        if e.ts_recv_ns == 1:
            raise ValueError("boom")
        seen.append(e.ts_recv_ns)

    async with EventBus() as bus:
        sub = bus.subscribe(Trade, flaky, name="flaky")
        for i in range(3):
            await bus.publish(trade(i))
        await sub.drain()
        assert sub.handler_errors == 1
    assert seen == [0, 2]
    assert "subscriber handler failed" in caplog.text


async def test_drop_oldest_keeps_newest_and_counts() -> None:
    gate = asyncio.Event()
    seen: list[int] = []

    async def slow(e: Trade) -> None:
        await gate.wait()
        seen.append(e.ts_recv_ns)

    async with EventBus() as bus:
        sub = bus.subscribe(Trade, slow, maxsize=3, overflow=Overflow.DROP_OLDEST)
        await bus.publish(trade(0))
        await asyncio.sleep(0)  # Consumer nimmt Event 0 und hängt am Gate
        for i in range(1, 11):
            await bus.publish(trade(i))
        assert sub.dropped == 7
        gate.set()
    assert seen == [0, 8, 9, 10]


async def test_block_applies_backpressure_without_loss() -> None:
    gate = asyncio.Event()
    seen: list[int] = []

    async def slow(e: Trade) -> None:
        await gate.wait()
        seen.append(e.ts_recv_ns)

    async with EventBus() as bus:
        sub = bus.subscribe(Trade, slow, maxsize=2, overflow=Overflow.BLOCK)

        async def producer() -> None:
            for i in range(10):
                await bus.publish(trade(i))

        task = asyncio.create_task(producer())
        await asyncio.sleep(0.01)
        assert not task.done(), "publish muss bei voller Queue warten"
        assert sub.pending == 2
        gate.set()
        await task
    assert seen == list(range(10))


async def test_slow_subscriber_does_not_block_drop_subscriber_isolation() -> None:
    """Ein langsamer DROP-Subscriber darf einen BLOCK-Subscriber nicht ausbremsen."""
    gate = asyncio.Event()
    fast: list[int] = []

    async def slow(e: Trade) -> None:
        await gate.wait()

    async def quick(e: Trade) -> None:
        fast.append(e.ts_recv_ns)

    async with EventBus() as bus:
        bus.subscribe(Trade, slow, maxsize=1, overflow=Overflow.DROP_OLDEST)
        q = bus.subscribe(Trade, quick)
        for i in range(100):
            await bus.publish(trade(i))
        await q.drain()
        assert fast == list(range(100))
        gate.set()


async def test_unsubscribe_stops_delivery() -> None:
    seen: list[int] = []

    async def h(e: Trade) -> None:
        seen.append(e.ts_recv_ns)

    async with EventBus() as bus:
        sub = bus.subscribe(Trade, h)
        await bus.publish(trade(1))
        await sub.drain()
        await sub.unsubscribe()
        await bus.publish(trade(2))
    assert seen == [1]


async def test_publish_after_close_raises() -> None:
    bus = EventBus()
    await bus.close()
    with pytest.raises(RuntimeError):
        await bus.publish(trade(1))


async def test_zero_maxsize_rejected() -> None:
    async def h(e: Trade) -> None:
        pass

    async with EventBus() as bus:
        with pytest.raises(ValueError, match="maxsize"):
            bus.subscribe(Trade, h, maxsize=0)
