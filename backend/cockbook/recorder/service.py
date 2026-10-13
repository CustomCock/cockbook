"""Recorder als Bus-Subscriber."""

from __future__ import annotations

import asyncio
import logging

from cockbook.config import RecorderConfig
from cockbook.core.eventbus import EventBus, Overflow, Subscription
from cockbook.core.events import RawMessage
from cockbook.recorder.writer import RecordWriter

log = logging.getLogger(__name__)


class Recorder:
    """Abonniert ``RawMessage`` mit ``Overflow.BLOCK`` – eine Aufzeichnung mit Lücken
    wäre für das Replay wertlos. Geschrieben wird gebündelt in einem Worker-Thread,
    damit Kompression und Disk-I/O die Event-Loop nicht blockieren."""

    def __init__(self, cfg: RecorderConfig, bus: EventBus) -> None:
        self.cfg = cfg
        self.writer = RecordWriter(cfg.data_dir, zstd_level=cfg.zstd_level)
        self._bus = bus
        self._sub: Subscription | None = None

    async def _on_raw(self, msg: RawMessage) -> None:
        self.writer.add(msg)

    async def flush(self) -> None:
        batch = self.writer.take()
        if batch:
            await asyncio.to_thread(self.writer.write_batch, batch)

    async def run(self, stop: asyncio.Event) -> None:
        self._sub = self._bus.subscribe(
            RawMessage, self._on_raw, name="recorder", overflow=Overflow.BLOCK
        )
        log.info("recorder started", extra={"data_dir": str(self.cfg.data_dir)})
        try:
            while not stop.is_set():
                try:
                    async with asyncio.timeout(self.cfg.flush_interval_s):
                        await stop.wait()
                except TimeoutError:
                    pass
                await self.flush()
        finally:
            # Auch bei Abbruch (Ctrl+C) alles Gepufferte schreiben.
            await self._sub.drain()
            await self.flush()
            log.info(
                "recorder stopped",
                extra={
                    "records": self.writer.records_written,
                    "bytes": self.writer.bytes_written,
                },
            )
