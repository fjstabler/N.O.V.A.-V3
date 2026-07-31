"""Asynchronous event bus.

Every subsystem communicates through this bus rather than holding references to
one another. That is the single rule that keeps the module graph acyclic: the
voice pipeline does not import the renderer bridge, the skills do not import the
orchestrator — they publish and subscribe.

Topics are dot-delimited (``voice.transcript.final``). Subscribers may use a
trailing ``*`` wildcard to match a subtree (``voice.*``) or ``*`` for everything.

Delivery is fire-and-forget: a slow or failing subscriber can never stall a
publisher, because each handler runs in its own task with its own error
boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .logging import get_logger

log = get_logger(__name__)

Handler = Callable[["Event"], Awaitable[None] | None]


@dataclass(slots=True, frozen=True)
class Event:
    """An immutable message on the bus."""

    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source: str | None = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


@dataclass(slots=True)
class _Subscription:
    pattern: str
    handler: Handler
    once: bool = False


class EventBus:
    """In-process publish/subscribe hub."""

    def __init__(self) -> None:
        self._subs: list[_Subscription] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # ------------------------------------------------------------------ subs

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        """Register ``handler`` for ``pattern``. Returns an unsubscribe callable."""
        sub = _Subscription(pattern, handler)
        self._subs.append(sub)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subs.remove(sub)

        return unsubscribe

    def on(self, pattern: str) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`subscribe`."""

        def decorator(handler: Handler) -> Handler:
            self.subscribe(pattern, handler)
            return handler

        return decorator

    async def wait_for(self, pattern: str, timeout: float | None = None) -> Event:
        """Block until an event matching ``pattern`` is published."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Event] = loop.create_future()

        def handler(event: Event) -> None:
            if not future.done():
                future.set_result(event)

        unsubscribe = self.subscribe(pattern, handler)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            unsubscribe()

    # --------------------------------------------------------------- publish

    def publish(
        self, topic: str, payload: dict[str, Any] | None = None, *, source: str | None = None
    ) -> Event:
        """Publish without waiting for subscribers to finish."""
        event = Event(topic=topic, payload=payload or {}, source=source)
        if self._closed:
            return event
        for sub in self._matching(topic):
            self._dispatch(sub, event)
        return event

    async def publish_and_wait(
        self, topic: str, payload: dict[str, Any] | None = None, *, source: str | None = None
    ) -> Event:
        """Publish and await every subscriber. Used by tests and shutdown paths."""
        event = Event(topic=topic, payload=payload or {}, source=source)
        if self._closed:
            return event
        coros = []
        for sub in self._matching(topic):
            result = self._invoke(sub, event)
            if result is not None:
                coros.append(result)
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        return event

    # -------------------------------------------------------------- internal

    def _matching(self, topic: str) -> Iterator[_Subscription]:
        # Snapshot: a handler is allowed to subscribe/unsubscribe during dispatch.
        for sub in tuple(self._subs):
            if sub.pattern == "*" or fnmatch.fnmatchcase(topic, sub.pattern):
                yield sub

    def _invoke(self, sub: _Subscription, event: Event) -> Awaitable[None] | None:
        try:
            result = sub.handler(event)
        except Exception:
            log.exception("event_handler_failed", topic=event.topic, pattern=sub.pattern)
            return None
        if asyncio.iscoroutine(result):
            return self._guard(result, sub, event)
        return None

    async def _guard(self, coro: Awaitable[None], sub: _Subscription, event: Event) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event_handler_failed", topic=event.topic, pattern=sub.pattern)

    def _dispatch(self, sub: _Subscription, event: Event) -> None:
        awaitable = self._invoke(sub, event)
        if awaitable is None:
            return
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for in-flight handlers — used during graceful shutdown."""
        if not self._tasks:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait(tuple(self._tasks), timeout=timeout)

    async def close(self) -> None:
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        await self.drain(timeout=2.0)
        self._subs.clear()


# --------------------------------------------------------------------- topics


class Topics:
    """Canonical topic names.

    Kept as constants so a typo is an ``AttributeError`` at import time rather
    than an event nobody receives at 3am.
    """

    # lifecycle
    READY = "system.ready"
    SHUTDOWN = "system.shutdown"
    SERVICE_HEALTH = "system.service.health"
    CAPABILITY_DEGRADED = "system.capability.degraded"

    # assistant state machine
    STATE_CHANGED = "state.changed"

    # voice
    WAKE_DETECTED = "voice.wake.detected"
    LISTEN_STARTED = "voice.listen.started"
    LISTEN_ENDED = "voice.listen.ended"
    AUDIO_LEVEL = "voice.audio.level"
    TRANSCRIPT_PARTIAL = "voice.transcript.partial"
    TRANSCRIPT_FINAL = "voice.transcript.final"
    SPEECH_STARTED = "voice.speech.started"
    SPEECH_ENDED = "voice.speech.ended"

    # reasoning
    TURN_STARTED = "assistant.turn.started"
    TURN_TEXT = "assistant.turn.text"
    TURN_COMPLETED = "assistant.turn.completed"
    TURN_FAILED = "assistant.turn.failed"
    TOOL_STARTED = "assistant.tool.started"
    TOOL_FINISHED = "assistant.tool.finished"

    # ui surfaces
    NOTIFICATION = "ui.notification"
    NOTIFICATION_DISMISS = "ui.notification.dismiss"
    CORE_PULSE = "ui.core.pulse"

    # telemetry + integrations
    METRICS = "system.metrics"
    SETTINGS_UPDATED = "settings.updated"
    HOME_EVENT = "home.event"
    CALENDAR_REMINDER = "calendar.reminder"
