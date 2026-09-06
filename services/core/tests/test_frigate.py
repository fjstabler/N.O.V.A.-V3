"""Frigate through Home Assistant: querying detections and alerting on them.

Frigate's detections arrive as ordinary HA entities and state-change events, so
all of this tests against a hand-built entity list and synthetic HOME_EVENTs —
no broker, no camera, no Frigate. Two things matter: reading which cameras see a
person right now, and turning a person *appearing* into a presence-routed alert.
"""

from __future__ import annotations

from nova.context import NovaContext
from nova.frigate_watch import FrigateService
from nova.integrations.frigate import (
    active_cameras,
    camera_name,
    count_sensors,
    detection_sensors,
    is_detection_event,
)
from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.integrations.services import HomeService
from nova.runtime import Topics
from nova.runtime.service import Service, ServiceState
from nova.skills.builtin.frigate import FrigateSkill


def sensor(entity_id: str, state: str) -> HAEntity:
    return HAEntity(entity_id=entity_id, state=state, attributes={})


HOUSE = [
    sensor("binary_sensor.front_door_person", "on"),
    sensor("binary_sensor.driveway_person", "off"),
    sensor("binary_sensor.front_door_motion", "on"),  # not an object sensor
    sensor("sensor.front_door_person", "2"),
    sensor("light.hallway", "on"),  # unrelated
]


# ------------------------------------------------------------------ helpers


def test_detection_sensors_pick_out_object_binary_sensors() -> None:
    found = {s.entity_id for s in detection_sensors(HOUSE, "person")}
    assert found == {"binary_sensor.front_door_person", "binary_sensor.driveway_person"}


def test_count_sensors_handle_both_naming_styles() -> None:
    entities = [sensor("sensor.yard_person", "1"), sensor("sensor.yard_person_count", "3")]
    assert len(count_sensors(entities, "person")) == 2


def test_camera_name_strips_the_object_suffix() -> None:
    assert camera_name("binary_sensor.front_door_person", "person") == "front door"
    assert camera_name("sensor.driveway_person_count", "person") == "driveway"


def test_active_cameras_reports_on_cameras_with_counts() -> None:
    active = active_cameras(HOUSE, "person")
    assert active == [("front door", 2)]  # driveway is off, front door on with count 2


def test_is_detection_event_only_fires_on_a_fresh_person() -> None:
    on = {
        "domain": "binary_sensor",
        "entityId": "binary_sensor.x_person",
        "state": "on",
        "previous": "off",
    }
    assert is_detection_event(on, "person") is True
    # already on (no transition), a motion sensor, or a light are all ignored
    assert is_detection_event({**on, "previous": "on"}, "person") is False
    assert is_detection_event({**on, "entityId": "binary_sensor.x_motion"}, "person") is False
    assert is_detection_event({**on, "domain": "light"}, "person") is False


# -------------------------------------------------------------------- skill


def running_home(ctx: NovaContext, *entities: HAEntity) -> HomeService:
    service = HomeService(ctx)
    ctx.services.register(service)
    client = HomeAssistantClient("http://ha.local:8123", "token")
    for ent in entities:
        client._entities[ent.entity_id] = ent
    service.ha = client
    service._set_state(ServiceState.RUNNING)
    return service


async def test_skill_needs_enabling_and_sensors(ctx: NovaContext) -> None:
    running_home(ctx, *HOUSE)
    # Disabled by default.
    assert FrigateSkill(ctx).is_available()[0] is False

    ctx.store.patch({"frigate": {"enabled": True}}, persist=False)
    assert FrigateSkill(ctx).is_available()[0] is True


async def test_skill_unavailable_without_person_sensors(ctx: NovaContext) -> None:
    ctx.store.patch({"frigate": {"enabled": True}}, persist=False)
    running_home(ctx, sensor("light.hallway", "on"))
    available, reason = FrigateSkill(ctx).is_available()
    assert available is False
    assert "sensors" in reason


async def test_check_cameras_reports_the_active_camera(ctx: NovaContext) -> None:
    ctx.store.patch({"frigate": {"enabled": True}}, persist=False)
    running_home(ctx, *HOUSE)

    result = await FrigateSkill(ctx).check_cameras()

    assert "front door" in result
    assert "2" in result


async def test_check_camera_distinguishes_empty_from_unknown(ctx: NovaContext) -> None:
    ctx.store.patch({"frigate": {"enabled": True}}, persist=False)
    running_home(ctx, *HOUSE)
    skill = FrigateSkill(ctx)

    assert "Yes" in await skill.check_camera(camera="front door")
    assert "No person" in await skill.check_camera(camera="driveway")
    assert "don't have" in await skill.check_camera(camera="attic")


# ------------------------------------------------------------------ service


class FakePresence(Service):
    name = "presence"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.reached: list[tuple[str, str, str]] = []

    async def reach_user(
        self, title: str, body: str = "", *, level: str = "info", click_url: str = ""
    ) -> str:
        self.reached.append((title, body, level))
        return "ok"


def make_service(ctx: NovaContext) -> tuple[FrigateService, FakePresence]:
    presence = FakePresence(ctx)
    ctx.services.register(presence)
    presence._set_state(ServiceState.RUNNING)
    service = FrigateService(ctx)
    ctx.services.register(service)
    service._set_state(ServiceState.RUNNING)
    return service, presence


PERSON_ON = {
    "domain": "binary_sensor",
    "entityId": "binary_sensor.front_door_person",
    "state": "on",
    "previous": "off",
}


async def test_a_detection_alerts_through_presence(ctx: NovaContext) -> None:
    ctx.store.patch({"frigate": {"enabled": True}}, persist=False)
    service, presence = make_service(ctx)
    await service.on_start()

    await ctx.bus.publish_and_wait(Topics.HOME_EVENT, dict(PERSON_ON), source="home")

    assert len(presence.reached) == 1
    title, body, level = presence.reached[0]
    assert "Front Door" in title
    assert "person" in body
    assert level == "warning"


async def test_alerts_respect_the_cooldown(ctx: NovaContext) -> None:
    ctx.store.patch({"frigate": {"enabled": True, "alert_cooldown_seconds": 3600}}, persist=False)
    service, presence = make_service(ctx)
    await service.on_start()

    await ctx.bus.publish_and_wait(Topics.HOME_EVENT, dict(PERSON_ON), source="home")
    await ctx.bus.publish_and_wait(Topics.HOME_EVENT, dict(PERSON_ON), source="home")

    assert len(presence.reached) == 1  # second suppressed by the cooldown


async def test_no_alert_when_disabled(ctx: NovaContext) -> None:
    service, presence = make_service(ctx)  # frigate disabled by default
    await service.on_start()

    await ctx.bus.publish_and_wait(Topics.HOME_EVENT, dict(PERSON_ON), source="home")

    assert presence.reached == []


async def test_camera_filter_limits_alerts(ctx: NovaContext) -> None:
    ctx.store.patch({"frigate": {"enabled": True, "cameras": ["driveway"]}}, persist=False)
    service, presence = make_service(ctx)
    await service.on_start()

    # front_door isn't in the allowed cameras → ignored.
    await ctx.bus.publish_and_wait(Topics.HOME_EVENT, dict(PERSON_ON), source="home")
    assert presence.reached == []

    driveway = {**PERSON_ON, "entityId": "binary_sensor.driveway_person"}
    await ctx.bus.publish_and_wait(Topics.HOME_EVENT, driveway, source="home")
    assert len(presence.reached) == 1
