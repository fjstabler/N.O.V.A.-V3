"""Request routing for the UI bridge.

Handlers are plain async callables registered against a :class:`Requests` topic.
Keeping every route in one table makes the entire UI-facing surface auditable at
a glance — nothing can be reached from the renderer that is not listed here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..runtime.logging import get_logger

log = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class RequestRouter:
    def __init__(self) -> None:
        self._routes: dict[str, Handler] = {}

    def register(self, topic: str, handler: Handler) -> None:
        if topic in self._routes:
            raise ValueError(f"duplicate route: {topic}")
        self._routes[topic] = handler

    def route(self, topic: str) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self.register(topic, handler)
            return handler

        return decorator

    def get(self, topic: str) -> Handler | None:
        return self._routes.get(topic)

    @property
    def topics(self) -> list[str]:
        return sorted(self._routes)
