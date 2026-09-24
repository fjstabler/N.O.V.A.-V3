"""The auto person-alarm: arm/disarm and the empty→occupied edge detection.

The alerting logic (baseline, confirm frames, one alert per visit, cooldown) is
driven directly through `_register`, so it needs no camera or model; the alert is
captured by a fake presence service. Arm/disarm guards are checked too.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.context import NovaContext
from nova.personwatch import PersonWatchService
from nova.runtime.errors import MissingDependency, SkillError
from nova.runtime.service import Service, ServiceState
from nova.skills.builtin.personwatch import PersonWatchSkill


class FakePresence(Service):
    name = "presence"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.reached: list[tuple[str, str]] = []

    async def reach_user(
        self, title: str, body: str = "", *, level: str = "info", click_url: str = ""
    ) -> str:
        self.reached.append((title, body))
        return "ok"


def make(ctx: NovaContext) -> tuple[PersonWatchService, FakePresence]:
    presence = FakePresence(ctx)
    ctx.services.register(presence)
    presence._set_state(ServiceState.RUNNING)
    service = PersonWatchService(ctx)
    ctx.services.register(service)
    service._set_state(ServiceState.RUNNING)
    return service, presence


# ---------------------------------------------------------- edge detection


async def test_alerts_when_someone_enters_an_empty_room(ctx: NovaContext) -> None:
    service, presence = make(ctx)  # confirm_frames defaults to 2

    await service._register(0)  # baseline: room empty
    assert presence.reached == []

    await service._register(1)  # someone appears — one frame, not yet confirmed
    assert presence.reached == []
    await service._register(1)  # confirmed → alert
    assert len(presence.reached) == 1
    assert "room" in presence.reached[0][0].lower()

    await service._register(1)  # still there — no repeat alert
    assert len(presence.reached) == 1


async def test_arming_with_someone_already_there_does_not_alert(ctx: NovaContext) -> None:
    """Arming as you leave (you're still in frame) must not alert on you."""
    service, presence = make(ctx)

    await service._register(1)  # baseline: a person is already present
    await service._register(1)
    await service._register(1)
    assert presence.reached == []  # never alerts on the baseline occupant


async def test_re_entry_alerts_again(ctx: NovaContext, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.store.patch(
        {"personwatch": {"confirm_frames": 1, "alert_cooldown_seconds": 5}}, persist=False
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr("nova.personwatch.time.monotonic", lambda: clock["t"])
    service, presence = make(ctx)

    await service._register(0)  # baseline empty
    await service._register(1)  # entry → alert
    assert len(presence.reached) == 1

    clock["t"] += 100  # let the cooldown pass
    await service._register(0)  # they leave
    await service._register(1)  # someone enters again → alert again
    assert len(presence.reached) == 2


async def test_cooldown_suppresses_a_quick_second_alert(ctx: NovaContext) -> None:
    ctx.store.patch(
        {"personwatch": {"confirm_frames": 1, "alert_cooldown_seconds": 3600}}, persist=False
    )
    service, presence = make(ctx)

    await service._register(0)
    await service._register(1)  # alert
    await service._register(0)  # leave
    await service._register(1)  # re-enter within cooldown → suppressed
    assert len(presence.reached) == 1


async def test_count_is_reflected_in_the_alert(ctx: NovaContext) -> None:
    ctx.store.patch({"personwatch": {"confirm_frames": 1}}, persist=False)
    service, presence = make(ctx)
    await service._register(0)
    await service._register(3)
    assert "3 people" in presence.reached[0][1]


# ----------------------------------------------------------------- control


async def test_arm_needs_the_camera_enabled(ctx: NovaContext) -> None:
    service, _ = make(ctx)  # camera disabled by default
    with pytest.raises(SkillError, match="camera is off"):
        await service.arm()


async def test_arm_reports_the_missing_person_extra(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    service, _ = make(ctx)

    def boom() -> None:
        raise MissingDependency("person detection", "ultralytics", "person")

    monkeypatch.setattr(service.detector, "ensure_ready", boom)
    with pytest.raises(SkillError, match="person detector isn't installed"):
        await service.arm()


async def test_arm_and_disarm(ctx: NovaContext, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    service, _ = make(ctx)
    monkeypatch.setattr(service.detector, "ensure_ready", lambda: None)

    async def fake_read(index: int) -> Any:
        raise RuntimeError("no real camera in the test")  # caught by the watch loop

    monkeypatch.setattr("nova.personwatch.local_camera_pool.read_bgr", fake_read)

    assert "watching" in (await service.arm()).lower()
    assert service.armed is True

    assert "stood down" in (await service.disarm()).lower()
    assert service.armed is False


# ------------------------------------------------------------------- skill


async def test_skill_gates_on_the_camera(ctx: NovaContext) -> None:
    make(ctx)
    assert PersonWatchSkill(ctx).is_available()[0] is False  # camera off
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    assert PersonWatchSkill(ctx).is_available()[0] is True


async def test_skill_arm_delegates(ctx: NovaContext, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    service, _ = make(ctx)
    monkeypatch.setattr(service.detector, "ensure_ready", lambda: None)

    async def fake_read(index: int) -> Any:
        raise RuntimeError("no camera")

    monkeypatch.setattr("nova.personwatch.local_camera_pool.read_bgr", fake_read)

    skill = PersonWatchSkill(ctx)
    await skill.arm_person_alarm()
    assert service.armed is True
    await skill.disarm_person_alarm()
    assert service.armed is False
