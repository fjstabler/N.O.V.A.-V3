"""SecuritySkill: the voice-facing arm/disarm/learn_face surface over
SecurityService — same split as HomeSkill sitting over HomeService.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.context import NovaContext
from nova.integrations.local_camera import local_camera_pool
from nova.runtime.errors import SkillError
from nova.security.faces import FaceObservation
from nova.security.service import SecurityService
from nova.skills.builtin.security import SecuritySkill


async def make_running_security(ctx: NovaContext) -> SecurityService:
    service = SecurityService(ctx)
    ctx.services.register(service)
    await service.start()
    service.engine._detector = object()
    service.engine._recogniser = object()
    return service


@pytest.fixture(autouse=True)
def fake_pool(monkeypatch: pytest.MonkeyPatch):
    async def fake_read_bgr(index: int) -> Any:
        return "frame"

    monkeypatch.setattr(local_camera_pool, "read_bgr", fake_read_bgr)


def configure_camera(ctx: NovaContext, name: str = "bedroom") -> None:
    ctx.store.patch({"vision": {"named_cameras": [{"name": name, "index": 0}]}}, persist=False)
    ctx.store.patch({"security": {"camera_name": name}}, persist=False)


async def test_unavailable_without_a_running_security_service(ctx: NovaContext) -> None:
    skill = SecuritySkill(ctx)
    available, reason = skill.is_available()
    assert available is False
    assert reason


async def test_available_once_the_service_is_registered(ctx: NovaContext) -> None:
    await make_running_security(ctx)
    assert SecuritySkill(ctx).is_available() == (True, "")


async def test_context_lines_report_stood_down_and_enrolled_count(ctx: NovaContext) -> None:
    service = await make_running_security(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    skill = SecuritySkill(ctx)

    lines = skill.context_lines()

    assert any("stood down" in line for line in lines)
    assert any("1 face" in line for line in lines)


async def test_context_lines_report_armed_and_which_camera(ctx: NovaContext) -> None:
    configure_camera(ctx)
    service = await make_running_security(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    await service.arm()
    skill = SecuritySkill(ctx)

    lines = skill.context_lines()

    assert any("armed" in line and "bedroom" in line for line in lines)
    await service.disarm()


async def test_arm_and_disarm_tools_delegate_to_the_service(ctx: NovaContext) -> None:
    configure_camera(ctx)
    service = await make_running_security(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    skill = SecuritySkill(ctx)

    assert await skill.arm_room_watch() == "Watching now."
    assert service.armed is True

    assert await skill.disarm_room_watch() == "Stood down."
    assert service.armed is False


async def test_learn_face_stores_an_embedding_under_the_given_name(ctx: NovaContext) -> None:
    configure_camera(ctx)
    service = await make_running_security(ctx)
    service.engine.observe = lambda frame: [  # type: ignore[method-assign]
        FaceObservation(bbox=(0, 0, 10, 10), embedding=[1.0, 0.0, 0.0, 0.0])
    ]
    skill = SecuritySkill(ctx)

    result = await skill.learn_face(name="Fin")

    assert "Fin" in result
    assert service.faces.names() == ["Fin"]


async def test_learn_face_picks_the_largest_face_when_several_are_in_frame(
    ctx: NovaContext,
) -> None:
    """The person actively enrolling is presumably the one filling more of
    the frame, not whoever else happens to be in the background."""
    configure_camera(ctx)
    service = await make_running_security(ctx)
    small = FaceObservation(bbox=(0, 0, 10, 10), embedding=[0.0, 1.0, 0.0, 0.0])
    large = FaceObservation(bbox=(0, 0, 100, 100), embedding=[1.0, 0.0, 0.0, 0.0])
    service.engine.observe = lambda frame: [small, large]  # type: ignore[method-assign]
    skill = SecuritySkill(ctx)

    await skill.learn_face(name="Fin")

    match = service.faces.match([1.0, 0.0, 0.0, 0.0], threshold=0.363)
    assert match is not None and match[0] == "Fin"


async def test_learn_face_without_a_camera_configured_raises_a_clear_error(
    ctx: NovaContext,
) -> None:
    await make_running_security(ctx)
    skill = SecuritySkill(ctx)

    with pytest.raises(SkillError, match="configured camera"):
        await skill.learn_face(name="Fin")


async def test_learn_face_with_no_visible_face_raises_a_clear_error(ctx: NovaContext) -> None:
    configure_camera(ctx)
    service = await make_running_security(ctx)
    service.engine.observe = lambda frame: []  # type: ignore[method-assign]
    skill = SecuritySkill(ctx)

    with pytest.raises(SkillError, match="couldn't see a face"):
        await skill.learn_face(name="Fin")
