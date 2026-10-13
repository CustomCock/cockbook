import json
from decimal import Decimal
from pathlib import Path

from cockbook.adapters.binance import (
    depth_stream_name,
    parse_agg_trade,
    parse_depth_snapshot,
    parse_depth_update,
    parse_force_order,
    parse_instrument,
    unwrap_combined,
)
from cockbook.core.events import Side

FIX = Path(__file__).parent / "fixtures" / "binance"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


INST = parse_instrument(json.loads(load("exchange_info.json")), "BTCUSDT")


def test_instrument_uses_price_filter_not_precision() -> None:
    assert INST.tick_size == Decimal("0.10")
    assert INST.step_size == Decimal("0.001")


def test_depth_update() -> None:
    stream, data = unwrap_combined(load("depth_update.json"))
    assert stream == "btcusdt@depth@100ms"
    u = parse_depth_update(data, INST, recv_ns=42)
    assert (u.first_id, u.final_id, u.prev_final_id) == (157, 160, 149)
    assert u.bids[0].price_ticks == 650001
    assert u.bids[1].qty == 0
    assert u.asks[0].qty == Decimal("0.004")
    assert u.recv_ns == 42


def test_depth_snapshot() -> None:
    s = parse_depth_snapshot(json.loads(load("depth_snapshot.json")), INST, 1)
    assert s.last_update_id == 1027024
    assert [lvl.price_ticks for lvl in s.bids] == [650001, 650000]


def test_agg_trade_buyer_is_maker_means_sell_aggressor() -> None:
    _, data = unwrap_combined(load("agg_trade_sell.json"))
    t = parse_agg_trade(data, INST, recv_ns=7)
    assert t.aggressor is Side.ASK
    assert t.price_ticks == 650001
    assert t.qty == Decimal("0.843")
    assert t.ts_exchange_ns == 1791923567355 * 1_000_000
    assert t.trade_id == "3483882303"


def test_agg_trade_buy_aggressor() -> None:
    _, data = unwrap_combined(load("agg_trade_sell.json"))
    data["m"] = False
    assert parse_agg_trade(data, INST, 0).aggressor is Side.BID


def test_force_order_long_liquidation_uses_avg_price() -> None:
    _, data = unwrap_combined(load("force_order.json"))
    liq = parse_force_order(data, INST, 0)
    assert liq.side is Side.ASK  # SELL = Long wird liquidiert
    assert liq.price_ticks == 649954  # ap 64995.37 gerundet auf 0.10
    assert liq.qty == Decimal("0.014")


def test_force_order_falls_back_to_order_price() -> None:
    _, data = unwrap_combined(load("force_order.json"))
    data["o"]["ap"] = "0"
    assert parse_force_order(data, INST, 0).price_ticks == 649900


def test_depth_stream_names() -> None:
    assert depth_stream_name("BTCUSDT", 100) == "btcusdt@depth@100ms"
    assert depth_stream_name("BTCUSDT", 250) == "btcusdt@depth"
    assert depth_stream_name("BTCUSDT", 500) == "btcusdt@depth@500ms"
