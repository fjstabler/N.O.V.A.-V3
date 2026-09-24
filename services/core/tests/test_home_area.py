"""Room-level control, the house overview and climate readout.

The reach beyond one-device-at-a-time: "turn off the bedroom" resolves an area,
gathers the right domains (never a cover or a lock), and turns them off in a
single service call. Overviews read straight off the live entity cache, so these
drive a HomeService populated by hand — no Home Assistant, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.context import NovaContext
from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.integrations.services import HomeService
from nova.runtime.errors import IntegrationError
from nova.runtime.service import ServiceState
from nova.skills.builtin.home import HomeSkill


def entity(entity_id: str, state: str, *, area: str = "", **attrs: Any) -> HAEntity:
    friendly = attrs.pop("friendly_name", entity_id.split(".", 1)[-1].replace("_", " ").title())
    return HAEntity(
        entity_id=entity_id,
        state=state,
        attributes={"friendly_name": friendly, "area": area, **attrs},
    )


def make_home(ctx: NovaContext, *entities: HAEntity) -> tuple[HomeService, list[tuple[str, Any]]]:
    service = HomeService(ctx)
    ctx.services.register(service)
    client = HomeAssistantClient("http://ha.local:8123", "token")
    for ent in entities:
        client._entities[ent.entity_id] = ent

    # set_many POSTs a service call directly (the payload's entity_id is a list),
    # so intercept the HTTP layer and record (path, payload) rather than a call.
    recorded: list[tuple[str, Any]] = []

    async def fake_post(path: str, payload: dict[str, Any]) -> list:
        recorded.append((path, payload))
        return []

    client._post = fake_post  # type: ignore[method-assign]
    service.ha = client
    service._set_state(ServiceState.RUNNING)
    return service, recorded


HOUSE = (
    entity("light.kitchen", "on", area="Kitchen", friendly_name="Kitchen Light"),
    entity("switch.kettle", "on", area="Kitchen", friendly_name="Kettle"),
    entity("light.bedroom", "off", area="Main Bedroom", friendly_name="Bedroom Light"),
    entity("fan.bedroom", "on", area="Main Bedroom", friendly_name="Bedroom Fan"),
    entity("cover.bedroom_blind", "open", area="Main Bedroom", friendly_name="Bedroom Blind"),
    entity("lock.front", "unlocked", area="Hallway", friendly_name="Front Door"),
    entity(
        "climate.hallway",
        "heat",
        area="Hallway",
        friendly_name="Thermostat",
        current_temperature=19.5,
        temperature=21.0,
    ),
    entity(
        "sensor.garden",
        "8.2",
        friendly_name="Garden Temp",
        device_class="temperature",
        unit_of_measurement="°C",
    ),
)


# ------------------------------------------------------------------ client


def test_areas_lists_every_distinct_room(ctx: NovaContext) -> None:
    service, _ = make_home(ctx, *HOUSE)
    assert service.ha.areas() == ["Hallway", "Kitchen", "Main Bedroom"]


def test_in_area_matches_exactly_and_by_substring(ctx: NovaContext) -> None:
    service, _ = make_home(ctx, *HOUSE)
    client = service.ha
    # substring: "bedroom" → "Main Bedroom"
    ids = {e.entity_id for e in client.in_area("bedroom")}
    assert ids == {"light.bedroom", "fan.bedroom", "cover.bedroom_blind"}
    # domain filter
    lights = client.in_area("bedroom", domains=("light",))
    assert [e.entity_id for e in lights] == ["light.bedroom"]


# -------------------------------------------------------------- room control


async def test_control_room_turns_off_only_the_right_domains(ctx: NovaContext) -> None:
    _service, recorded = make_home(ctx, *HOUSE)

    result = await HomeSkill(ctx).control_room(room="bedroom", action="off")

    # The blind (cover) is never swept up in a blanket room-off.
    assert len(recorded) == 1
    path, payload = recorded[0]
    assert path == "/services/homeassistant/turn_off"
    assert set(payload["entity_id"]) == {"light.bedroom", "fan.bedroom"}
    assert "2 devices in Main Bedroom" in result


async def test_control_room_can_limit_to_one_kind(ctx: NovaContext) -> None:
    _service, recorded = make_home(ctx, *HOUSE)

    await HomeSkill(ctx).control_room(room="kitchen", action="on", only="light")

    path, payload = recorded[0]
    assert path == "/services/homeassistant/turn_on"
    assert payload["entity_id"] == ["light.kitchen"]


async def test_control_room_reports_known_rooms_when_it_cannot_match(ctx: NovaContext) -> None:
    make_home(ctx, *HOUSE)
    with pytest.raises(IntegrationError, match="Rooms I know"):
        await HomeSkill(ctx).control_room(room="attic", action="off")


# ---------------------------------------------------------------- overviews


async def test_home_overview_reports_on_open_and_unlocked(ctx: NovaContext) -> None:
    make_home(ctx, *HOUSE)

    overview = await HomeSkill(ctx).home_overview()

    assert "3 on" in overview  # kitchen light, kettle, bedroom fan
    assert "Thermostat 19.5°" in overview
    assert "Bedroom Blind" in overview  # open cover
    assert "Front Door" in overview  # unlocked lock


async def test_home_overview_says_nothing_is_on_when_quiet(ctx: NovaContext) -> None:
    make_home(ctx, entity("light.x", "off", area="Den"))
    overview = await HomeSkill(ctx).home_overview()
    assert "Nothing is on" in overview


async def test_climate_summary_reads_thermostats_and_temperature_sensors(ctx: NovaContext) -> None:
    make_home(ctx, *HOUSE)

    summary = await HomeSkill(ctx).climate_summary()

    assert "Thermostat now 19.5° set to 21.0° (heat)" in summary
    assert "Garden Temp: 8.2°C" in summary
