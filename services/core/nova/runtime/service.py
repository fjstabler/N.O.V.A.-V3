"""Service lifecycle: the contract every subsystem implements.

A service declares what it depends on; the supervisor topologically sorts them,
starts them in order, and tears them down in reverse. A service that raises
:class:`DegradedCapability` during start is marked ``degraded`` and skipped —
the assistant boots without a microphone rather than not booting at all.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .errors import DegradedCapability, ServiceStartupError
from .events import EventBus, Topics
from .logging import get_logger

if TYPE_CHECKING:
    from ..context import NovaContext

log = get_logger(__name__)


class ServiceState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(slots=True)
class ServiceHealth:
    name: str
    state: ServiceState
    detail: str | None = None
    since: float = field(default_factory=time.time)
    restarts: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state.value,
            "detail": self.detail,
            "since": self.since,
            "restarts": self.restarts,
        }


class Service(ABC):
    """Base class for every long-lived subsystem.

    Subclasses override :meth:`on_start` / :meth:`on_stop`. Anything that needs a
    background loop should use :meth:`spawn`, which ties the task's lifetime to
    the service so shutdown is deterministic.
    """

    #: Unique service name, used for health reporting and dependency edges.
    name: str = "service"
    #: Names of services that must be RUNNING before this one starts.
    requires: tuple[str, ...] = ()
    #: If True, a start failure aborts boot instead of degrading.
    critical: bool = False

    def __init__(self, ctx: NovaContext) -> None:
        self.ctx = ctx
        self.bus: EventBus = ctx.bus
        self.log = get_logger(f"nova.{self.name}")
        self.health = ServiceHealth(name=self.name, state=ServiceState.CREATED)
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------- overrides

    async def on_start(self) -> None:
        """Acquire resources. Raise DegradedCapability to opt out gracefully."""

    async def on_stop(self) -> None:
        """Release resources. Must not raise."""

    def describe(self) -> str | None:
        """Optional one-line status shown in the settings panel."""
        return None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> ServiceState:
        self._set_state(ServiceState.STARTING)
        try:
            await self.on_start()
        except DegradedCapability as exc:
            self._set_state(ServiceState.DEGRADED, exc.reason)
            self.log.warning("service_degraded", reason=exc.reason, remedy=exc.remedy)
            self.bus.publish(Topics.CAPABILITY_DEGRADED, exc.as_payload(), source=self.name)
            return self.health.state
        except Exception as exc:
            self._set_state(ServiceState.FAILED, str(exc))
            self.log.exception("service_start_failed")
            if self.critical:
                raise ServiceStartupError(
                    f"critical service '{self.name}' failed to start"
                ) from exc
            return self.health.state
        self._set_state(ServiceState.RUNNING, self.describe())
        return self.health.state

    async def stop(self) -> None:
        if self.health.state in (ServiceState.STOPPED, ServiceState.CREATED):
            return
        self._set_state(ServiceState.STOPPING)
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        try:
            await self.on_stop()
        except Exception:
            self.log.exception("service_stop_failed")
        self._set_state(ServiceState.STOPPED)

    @property
    def running(self) -> bool:
        return self.health.state is ServiceState.RUNNING

    # ------------------------------------------------------------- utilities

    def spawn(self, coro: object, *, name: str | None = None) -> asyncio.Task[None]:
        """Run a background loop bound to this service's lifetime."""
        task = asyncio.create_task(coro, name=name or f"{self.name}-task")  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.log.error("background_task_crashed", task=task.get_name(), error=str(exc))

    def _set_state(self, state: ServiceState, detail: str | None = None) -> None:
        self.health.state = state
        self.health.detail = detail
        self.health.since = time.time()
        self.bus.publish(Topics.SERVICE_HEALTH, self.health.as_payload(), source=self.name)


class ServiceManager:
    """Starts, stops and reports on the service graph."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._services: dict[str, Service] = {}
        self._order: list[str] = []

    def register(self, service: Service) -> Service:
        if service.name in self._services:
            raise ValueError(f"duplicate service name: {service.name}")
        self._services[service.name] = service
        return service

    def get(self, name: str) -> Service | None:
        return self._services.get(name)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._services.values())

    def resolve_order(self) -> list[str]:
        """Depth-first topological sort; raises on a dependency cycle."""
        order: list[str] = []
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(name: str, trail: tuple[str, ...]) -> None:
            if name in permanent:
                return
            if name in temporary:
                cycle = " -> ".join((*trail, name))
                raise ServiceStartupError(f"service dependency cycle: {cycle}")
            temporary.add(name)
            service = self._services.get(name)
            if service is None:
                raise ServiceStartupError(f"unknown service dependency: {name}")
            for dep in service.requires:
                visit(dep, (*trail, name))
            temporary.discard(name)
            permanent.add(name)
            order.append(name)

        for name in self._services:
            visit(name, ())
        return order

    async def start_all(self) -> None:
        self._order = self.resolve_order()
        for name in self._order:
            service = self._services[name]
            # A service whose dependency never came up cannot run either.
            unmet = [d for d in service.requires if not self._services[d].running]
            if unmet:
                service._set_state(ServiceState.DEGRADED, f"requires {', '.join(unmet)}")
                service.log.warning("service_skipped", unmet=unmet)
                continue
            started = time.perf_counter()
            await service.start()
            log.info(
                "service_started",
                service=name,
                state=service.health.state.value,
                ms=round((time.perf_counter() - started) * 1000),
            )

    async def stop_all(self) -> None:
        for name in reversed(self._order or list(self._services)):
            service = self._services.get(name)
            if service is not None:
                await service.stop()

    async def restart(self, name: str) -> ServiceState:
        """Stop and start one service, picking up its current configuration.

        Integration services read their settings once, at start. Without this a
        newly-entered URL or token does nothing until the whole process is
        restarted — the save appears to succeed and the integration stays dark,
        which is indistinguishable from it being broken.

        A degraded service restarts too: that is the case that matters, since it
        is precisely what an unconfigured integration looks like.
        """
        service = self._services.get(name)
        if service is None:
            raise ServiceStartupError(f"unknown service: {name}")
        log.info("service_restarting", service=name, was=service.health.state.value)
        await service.stop()
        state = await service.start()
        log.info("service_restarted", service=name, state=state.value)
        return state

    def health_report(self) -> list[dict[str, object]]:
        return [s.health.as_payload() for s in self._services.values()]
