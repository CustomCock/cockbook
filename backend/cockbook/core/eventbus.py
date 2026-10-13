"""Asynchroner Pub/Sub für normalisierte Events.

Aufbau
------
Jeder Subscriber bekommt eine *eigene* begrenzte Queue und einen eigenen
Consumer-Task. ``publish`` legt das Event in die Queue jedes passenden Subscribers,
der Consumer-Task ruft den Handler auf. Zustellung an Subscriber einer Basisklasse
(z. B. ``Event``) erfolgt über die MRO – wer ``Event`` abonniert, bekommt alles.

Backpressure – warum pro Subscriber und nicht global
----------------------------------------------------
Die naheliegende Lösung wäre eine globale Queue oder ``await handler(event)`` direkt
in ``publish``. Dann bremst aber der langsamste Consumer (z. B. ein Frontend-Client
mit schlechter Verbindung) *alle* anderen aus – inklusive Orderbuch und Recorder.
Deshalb isolieren wir Subscriber voneinander und lassen jeden selbst wählen, was bei
voller Queue passiert:

- ``Overflow.BLOCK``: ``publish`` wartet, bis Platz ist. Für Consumer, bei denen
  Datenverlust das Ergebnis *falsch* macht: Orderbuch (ein fehlendes Delta =
  korruptes Buch), Recorder (Lücke in der Aufzeichnung). Der Druck wandert damit
  bis zum Adapter zurück; der liest den Socket langsamer, die Börse puffert bzw.
  trennt irgendwann – und dann greift die normale Resync-Logik. Das ist ein
  *sichtbarer* Fehler statt stiller Korruption.
- ``Overflow.DROP_OLDEST``: das älteste Event fliegt raus. Für Consumer, die nur
  den aktuellen Zustand brauchen (UI-Snapshots, Anzeige-Metriken). Verworfene Events
  werden gezählt und geloggt.

Die Queues sind immer begrenzt: eine unbegrenzte Queue versteckt das Problem nur,
bis der RAM voll ist.

Fehler in Handlern werden geloggt und beenden den Consumer nicht – ein kaputter
Indikator darf nicht den Rest lahmlegen.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from cockbook.core.events import Event

log = logging.getLogger(__name__)

type Handler[E: Event] = Callable[[E], Awaitable[None]]


class Overflow(enum.Enum):
    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"


class Subscription:
    """Handle auf ein Abo. Wird von :meth:`EventBus.subscribe` erzeugt."""

    def __init__(
        self,
        bus: EventBus,
        event_type: type[Event],
        handler: Callable[[Any], Awaitable[None]],
        *,
        name: str,
        maxsize: int,
        overflow: Overflow,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize muss > 0 sein (unbegrenzte Queues sind nicht erlaubt)")
        self._bus = bus
        self.event_type = event_type
        self.name = name
        self.overflow = overflow
        self.dropped = 0
        self.handler_errors = 0
        self._handler = handler
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._task = asyncio.create_task(self._run(), name=f"eventbus:{name}")

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def _put(self, event: Event) -> None:
        if self.overflow is Overflow.BLOCK:
            await self._queue.put(event)
            return
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
            self.dropped += 1
            # Log nicht fluten: erstes Drop und danach jedes 1000.
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning(
                    "subscriber overflow, dropping oldest events",
                    extra={"subscriber": self.name, "dropped_total": self.dropped},
                )
        self._queue.put_nowait(event)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.handler_errors += 1
                log.exception("subscriber handler failed", extra={"subscriber": self.name})
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wartet, bis alle bereits eingereihten Events verarbeitet wurden."""
        await self._queue.join()

    async def _stop(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def unsubscribe(self) -> None:
        await self._bus._remove(self)


class EventBus:
    def __init__(self, *, default_maxsize: int = 10_000) -> None:
        self._default_maxsize = default_maxsize
        self._subs: dict[type[Event], list[Subscription]] = {}
        # Cache: konkreter Event-Typ -> alle zuständigen Subscriptions (inkl. Basisklassen).
        self._route_cache: dict[type[Event], tuple[Subscription, ...]] = {}
        self._closed = False

    def subscribe[E: Event](
        self,
        event_type: type[E],
        handler: Handler[E],
        *,
        name: str | None = None,
        maxsize: int | None = None,
        overflow: Overflow = Overflow.BLOCK,
    ) -> Subscription:
        """Abonniert ``event_type`` (und alle Unterklassen). Braucht eine laufende Loop."""
        if self._closed:
            raise RuntimeError("EventBus ist geschlossen")
        sub = Subscription(
            self,
            event_type,
            handler,
            name=name or str(getattr(handler, "__qualname__", repr(handler))),
            maxsize=maxsize if maxsize is not None else self._default_maxsize,
            overflow=overflow,
        )
        self._subs.setdefault(event_type, []).append(sub)
        self._route_cache.clear()
        return sub

    def _routes(self, event_type: type[Event]) -> tuple[Subscription, ...]:
        routes = self._route_cache.get(event_type)
        if routes is None:
            routes = tuple(sub for cls in event_type.__mro__ for sub in self._subs.get(cls, ()))
            self._route_cache[event_type] = routes
        return routes

    async def publish(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("EventBus ist geschlossen")
        for sub in self._routes(type(event)):
            await sub._put(event)

    async def _remove(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.event_type, [])
        if sub in subs:
            subs.remove(sub)
            self._route_cache.clear()
        await sub._stop()

    async def close(self, *, drain_timeout: float | None = 5.0) -> None:
        """Stoppt alle Consumer. Versucht vorher, offene Events abzuarbeiten."""
        if self._closed:
            return
        self._closed = True
        all_subs = [s for subs in self._subs.values() for s in subs]
        if drain_timeout:
            try:
                async with asyncio.timeout(drain_timeout):
                    await asyncio.gather(*(s.drain() for s in all_subs))
            except TimeoutError:
                log.warning("eventbus drain timed out, discarding pending events")
        await asyncio.gather(*(s._stop() for s in all_subs))
        self._subs.clear()
        self._route_cache.clear()

    async def __aenter__(self) -> EventBus:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
