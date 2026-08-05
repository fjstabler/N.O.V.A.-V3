"""DisplaySkill: putting a map or a camera on screen rather than describing one.

`show_camera` resolves against two sources under one name — a locally
configured camera (`vision.named_cameras`) and a Home Assistant `camera.*`
entity — and both have to publish the same shaped `ui.surface.show` event so
the frontend does not need to know which one actually answered.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.context import NovaContext
from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.integrations.services import HomeService
from nova.runtime.errors import SkillError
from nova.runtime.service import ServiceState
from nova.skills.builtin.display import DisplaySkill

front_door = HAEntity(
    entity_id="camera.front_door",
    state="idle",
    attributes={"friendly_name": "Front Door"},
)


def make_running_home(ctx: NovaContext, **entities: HAEntity) -> HomeService:
    service = HomeService(ctx)
    ctx.services.register(service)
    client = HomeAssistantClient("http://ha.local:8123", "token")
    client._entities.update(entities)
    service.ha = client
    service._set_state(ServiceState.RUNNING)
    return service


def captured_events(ctx: NovaContext, topic: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ctx.bus.subscribe(topic, lambda e: events.append(e.payload))
    return events


# ------------------------------------------------------------------- camera


async def test_show_camera_resolves_a_named_local_camera(ctx: NovaContext) -> None:
    ctx.store.patch({"vision": {"named_cameras": [{"name": "bedroom", "index": 2}]}}, persist=False)
    events = captured_events(ctx, "ui.surface.show")

    result = await DisplaySkill(ctx).show_camera(name="bedroom")

    assert "bedroom" in result.lower()
    assert events == [
        {"kind": "camera", "title": "bedroom", "streamPath": "/camera/local%3Abedroom"}
    ]


async def test_show_camera_falls_back_to_a_home_assistant_entity(ctx: NovaContext) -> None:
    make_running_home(ctx, **{front_door.entity_id: front_door})
    events = captured_events(ctx, "ui.surface.show")

    result = await DisplaySkill(ctx).show_camera(name="front door")

    assert "Front Door" in result
    assert events == [
        {"kind": "camera", "title": "Front Door", "streamPath": "/camera/ha%3Acamera.front_door"}
    ]


async def test_a_local_name_takes_priority_over_a_same_named_ha_camera(ctx: NovaContext) -> None:
    """Regression against ambiguity: if both a configured local camera and an
    HA entity could answer "bedroom", the one the user explicitly named in
    settings should win, not whichever fuzzy match HA happens to produce."""
    bedroom_ha = HAEntity(
        entity_id="camera.bedroom_wyze", state="idle", attributes={"friendly_name": "Bedroom"}
    )
    make_running_home(ctx, **{bedroom_ha.entity_id: bedroom_ha})
    ctx.store.patch({"vision": {"named_cameras": [{"name": "bedroom", "index": 0}]}}, persist=False)

    events = captured_events(ctx, "ui.surface.show")
    await DisplaySkill(ctx).show_camera(name="bedroom")

    assert events[0]["streamPath"] == "/camera/local%3Abedroom"


async def test_show_camera_raises_a_clear_error_when_nothing_resolves(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="isn't a configured camera"):
        await DisplaySkill(ctx).show_camera(name="attic")


# ---------------------------------------------------------------------- map


async def test_show_map_geocodes_and_publishes_a_point(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = captured_events(ctx, "ui.surface.show")

    async def fake_geocode(self: DisplaySkill, location: str) -> tuple[float, float, str]:
        assert location == "London"
        return 51.5074, -0.1278, "London"

    monkeypatch.setattr(DisplaySkill, "_geocode", fake_geocode)

    result = await DisplaySkill(ctx).show_map(location="London")

    assert "London" in result
    assert events == [{"kind": "map", "title": "London", "lat": 51.5074, "lon": -0.1278}]


async def test_show_map_raises_when_nothing_is_found(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty_geocode(self: DisplaySkill, location: str) -> tuple[float, float, str]:
        raise SkillError(f"I couldn't find '{location}' on the map.")

    monkeypatch.setattr(DisplaySkill, "_geocode", empty_geocode)

    with pytest.raises(SkillError, match="couldn't find"):
        await DisplaySkill(ctx).show_map(location="Nowhereville")
