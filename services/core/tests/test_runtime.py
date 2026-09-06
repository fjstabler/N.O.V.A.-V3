"""Event bus, state machine and service lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from nova.runtime import EventBus, NovaState, ServiceState, StateMachine
from nova.runtime.errors import DegradedCapability, ServiceStartupError
from nova.runtime.service import Service, ServiceManager

# ------------------------------------------------------------------ event bus


async def test_publish_reaches_matching_subscribers() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("voice.transcript.final", lambda e: seen.append(e.payload["text"]))
    await bus.publish_and_wait("voice.transcript.final", {"text": "hello"})
    assert seen == ["hello"]


async def test_wildcard_patterns() -> None:
    bus = EventBus()
    subtree: list[str] = []
    everything: list[str] = []
    bus.subscribe("voice.*", lambda e: subtree.append(e.topic))
    bus.subscribe("*", lambda e: everything.append(e.topic))

    await bus.publish_and_wait("voice.wake.detected")
    await bus.publish_and_wait("system.metrics")

    assert subtree == ["voice.wake.detected"]
    assert everything == ["voice.wake.detected", "system.metrics"]


async def test_async_handlers_are_awaited() -> None:
    bus = EventBus()
    seen: list[int] = []

    async def handler(event) -> None:  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        seen.append(event.payload["n"])

    bus.subscribe("tick", handler)
    await bus.publish_and_wait("tick", {"n": 1})
    assert seen == [1]


async def test_a_failing_handler_cannot_break_the_others() -> None:
    """One bad subscriber must never silence the rest of the system."""
    bus = EventBus()
    survivors: list[str] = []

    def explode(_event) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    bus.subscribe("topic", explode)
    bus.subscribe("topic", lambda e: survivors.append("ok"))
    await bus.publish_and_wait("topic")
    assert survivors == ["ok"]


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    seen: list[str] = []
    unsubscribe = bus.subscribe("topic", lambda e: seen.append("x"))
    await bus.publish_and_wait("topic")
    unsubscribe()
    await bus.publish_and_wait("topic")
    assert len(seen) == 1


async def test_wait_for_resolves_on_the_next_matching_event() -> None:
    bus = EventBus()

    async def publish_later() -> None:
        await asyncio.sleep(0.01)
        bus.publish("ready", {"value": 42})

    asyncio.create_task(publish_later())
    event = await bus.wait_for("ready", timeout=1.0)
    assert event.payload["value"] == 42


async def test_wait_for_times_out() -> None:
    bus = EventBus()
    with pytest.raises(asyncio.TimeoutError):
        await bus.wait_for("never", timeout=0.02)


# --------------------------------------------------------------- state machine


async def test_legal_transitions_are_applied() -> None:
    machine = StateMachine(EventBus())
    assert await machine.transition(NovaState.IDLE)
    assert await machine.transition(NovaState.LISTENING)
    assert machine.state is NovaState.LISTENING
    assert machine.previous is NovaState.IDLE


async def test_illegal_transitions_are_refused() -> None:
    machine = StateMachine(EventBus())
    await machine.transition(NovaState.IDLE)
    await machine.transition(NovaState.LISTENING)
    # Listening cannot jump straight to speaking; it must reason first.
    assert not await machine.transition(NovaState.SPEAKING)
    assert machine.state is NovaState.LISTENING


async def test_transitions_publish_an_event() -> None:
    bus = EventBus()
    machine = StateMachine(bus)
    seen: list[dict] = []
    bus.subscribe("state.changed", lambda e: seen.append(e.payload))
    await machine.transition(NovaState.IDLE, reason="boot")
    assert seen[0]["state"] == "idle"
    assert seen[0]["reason"] == "boot"


async def test_transient_states_revert_on_their_own() -> None:
    """An error is an overlay, not a mode — it must clear itself."""
    import nova.runtime.state as state_module

    original = state_module._TRANSIENT[NovaState.ERROR]
    state_module._TRANSIENT[NovaState.ERROR] = 0.02
    try:
        machine = StateMachine(EventBus())
        await machine.transition(NovaState.IDLE)
        await machine.transition(NovaState.ERROR)
        assert machine.state is NovaState.ERROR
        await asyncio.sleep(0.06)
        assert machine.state is NovaState.IDLE
    finally:
        state_module._TRANSIENT[NovaState.ERROR] = original


async def test_transition_to_current_state_is_a_noop() -> None:
    machine = StateMachine(EventBus())
    await machine.transition(NovaState.IDLE)
    assert await machine.transition(NovaState.IDLE)
    assert machine.state is NovaState.IDLE


# ------------------------------------------------------------------- services


class _Recorder(Service):
    name = "recorder"

    def __init__(self, ctx) -> None:  # type: ignore[no-untyped-def]
        super().__init__(ctx)
        self.started = False

    async def on_start(self) -> None:
        self.started = True


def _fake_ctx(bus: EventBus):  # type: ignore[no-untyped-def]
    class Ctx:
        def __init__(self) -> None:
            self.bus = bus

    return Ctx()


async def test_dependencies_decide_start_order() -> None:
    bus = EventBus()
    ctx = _fake_ctx(bus)
    order: list[str] = []

    def make(service_name: str, deps: tuple[str, ...]) -> Service:
        class Dynamic(Service):
            name = service_name
            requires = deps

            async def on_start(self) -> None:
                order.append(service_name)

        return Dynamic(ctx)  # type: ignore[arg-type]

    manager = ServiceManager(bus)
    manager.register(make("c", ("b",)))
    manager.register(make("a", ()))
    manager.register(make("b", ("a",)))
    await manager.start_all()
    assert order == ["a", "b", "c"]


async def test_dependency_cycles_are_rejected() -> None:
    bus = EventBus()
    ctx = _fake_ctx(bus)

    def make(service_name: str, deps: tuple[str, ...]) -> Service:
        class Dynamic(Service):
            name = service_name
            requires = deps

        return Dynamic(ctx)  # type: ignore[arg-type]

    manager = ServiceManager(bus)
    manager.register(make("a", ("b",)))
    manager.register(make("b", ("a",)))
    with pytest.raises(ServiceStartupError, match="cycle"):
        manager.resolve_order()


async def test_a_degraded_dependency_degrades_its_dependants() -> None:
    """Boot must continue: no microphone should not mean no assistant."""
    bus = EventBus()
    ctx = _fake_ctx(bus)

    class Optional(Service):
        name = "optional"

        async def on_start(self) -> None:
            raise DegradedCapability("optional", "hardware missing")

    class Dependant(Service):
        name = "dependant"
        requires = ("optional",)

    manager = ServiceManager(bus)
    manager.register(Optional(ctx))  # type: ignore[arg-type]
    manager.register(Dependant(ctx))  # type: ignore[arg-type]
    await manager.start_all()

    assert manager.get("optional").health.state is ServiceState.DEGRADED
    assert manager.get("dependant").health.state is ServiceState.DEGRADED


async def test_a_critical_service_failure_aborts_boot() -> None:
    bus = EventBus()
    ctx = _fake_ctx(bus)

    class Critical(Service):
        name = "critical"
        critical = True

        async def on_start(self) -> None:
            raise RuntimeError("port in use")

    manager = ServiceManager(bus)
    manager.register(Critical(ctx))  # type: ignore[arg-type]
    with pytest.raises(ServiceStartupError):
        await manager.start_all()


async def test_stop_cancels_background_tasks() -> None:
    bus = EventBus()
    ctx = _fake_ctx(bus)
    ran = asyncio.Event()

    class Looping(Service):
        name = "looping"

        async def on_start(self) -> None:
            self.spawn(self._loop())

        async def _loop(self) -> None:
            ran.set()
            while True:
                await asyncio.sleep(0.01)

    service = Looping(ctx)  # type: ignore[arg-type]
    await service.start()
    await asyncio.wait_for(ran.wait(), timeout=1)
    await service.stop()
    assert service.health.state is ServiceState.STOPPED
