import asyncio
import json
from pathlib import Path

from cockbook.config import RecorderConfig
from cockbook.core.eventbus import EventBus
from cockbook.core.events import RawMessage
from cockbook.recorder.reader import iter_records
from cockbook.recorder.service import Recorder
from cockbook.recorder.writer import RecordWriter, encode_line, hour_key

H = 3600 * 10**9
T0 = 1_791_849_600 * 10**9  # 2026-10-13T00:00:00Z


def raw(
    t: int, stream: str = "depth", payload: str = '{"e":"depthUpdate","p":"65000.10"}'
) -> RawMessage:
    return RawMessage(
        exchange="binance_usdm", symbol="BTCUSDT", ts_recv_ns=t, stream=stream, payload=payload
    )


def test_hour_key_exact_at_boundary() -> None:
    assert hour_key(T0 + H - 1) == "2026-10-13T00"
    assert hour_key(T0 + H) == "2026-10-13T01"


def test_line_embeds_payload_verbatim() -> None:
    line = encode_line(raw(5, payload='{"p":"65000.10","q":"1.000"}'))
    assert line == '{"t":5,"s":"depth","d":{"p":"65000.10","q":"1.000"}}\n'
    assert json.loads(line)["d"]["p"] == "65000.10"  # String bleibt String, keine float-Rundung


def test_newlines_in_payload_keep_one_line() -> None:
    line = encode_line(raw(1, payload='{\n  "a": 1\n}'))
    assert line.count("\n") == 1
    assert json.loads(line)["d"] == {"a": 1}


def test_hourly_files_and_roundtrip(tmp_path: Path) -> None:
    w = RecordWriter(tmp_path)
    w.write_batch([raw(T0 + 1), raw(T0 + H - 1, "aggTrade"), raw(T0 + H)])
    base = tmp_path / "binance_usdm" / "BTCUSDT"
    files = sorted(p.name for p in base.iterdir())
    assert files == ["2026-10-13T00.jsonl.zst", "2026-10-13T01.jsonl.zst"]
    recs = list(iter_records(base / files[0]))
    assert [(r.recv_ns, r.stream) for r in recs] == [(T0 + 1, "depth"), (T0 + H - 1, "aggTrade")]
    assert recs[0].data["p"] == "65000.10"


def test_appending_frames_keeps_file_readable(tmp_path: Path) -> None:
    """Jeder Flush = eigener zstd-Frame; Neustart in derselben Stunde hängt an."""
    RecordWriter(tmp_path).write_batch([raw(T0 + 1)])
    RecordWriter(tmp_path).write_batch([raw(T0 + 2), raw(T0 + 3)])
    path = tmp_path / "binance_usdm" / "BTCUSDT" / f"{hour_key(T0)}.jsonl.zst"
    assert [r.recv_ns for r in iter_records(path)] == [T0 + 1, T0 + 2, T0 + 3]


async def test_recorder_service_flushes_on_stop(tmp_path: Path) -> None:
    cfg = RecorderConfig(data_dir=tmp_path, flush_interval_s=60)
    async with EventBus() as bus:
        rec = Recorder(cfg, bus)
        stop = asyncio.Event()
        task = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0)
        for i in range(100):
            await bus.publish(raw(T0 + i))
        stop.set()
        await task
    path = tmp_path / "binance_usdm" / "BTCUSDT" / f"{hour_key(T0)}.jsonl.zst"
    assert len(list(iter_records(path))) == 100
