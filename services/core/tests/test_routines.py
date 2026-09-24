"""Alexa-style routines: a saved phrase that runs several home actions at once.

Covered here: a routine is saved from model-emitted JSON steps, replays each step
against Home Assistant (a device, or a whole room, plus brightness/scene/…), and
reports honestly when a step's target can't be found. The HTTP layer is faked, so
the assertions are about which service calls a routine actually makes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nova.context import NovaContext
from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.integrations.services import HomeService
from nova.runtime.errors import IntegrationError
from nova.runtime.service import ServiceState
from nova.skills.builtin.home import HomeSkill


def entity(entity_id: str, state: str = "off", *, area: str = "", **attrs: Any) -> HAEntity:
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

    posts: list[tuple[str, Any]] = []

    async def fake_post(path: str, payload: dict[str, Any]) -> list:
        posts.append((path, payload))
        return []

    client._post = fake_post  # type: ignore[method-assign]
    service.ha = client
    service._set_state(ServiceState.RUNNING)
    return service, posts


LOUNGE = (
    entity("light.lounge_1", "on", area="Living Room", friendly_name="Lounge Lamp"),
    entity("light.lounge_2", "on", area="Living Room", friendly_name="Lounge Ceiling"),
    entity("media_player.tv", "off", area="Living Room", friendly_name="TV"),
    entity("light.kitchen", "on", area="Kitchen", friendly_name="Kitchen Light"),
)


# ------------------------------------------------------------------ create


async def test_create_routine_saves_and_persists_steps(ctx: NovaContext) -> None:
    make_home(ctx, *LOUNGE)
    steps = json.dumps(
        [{"action": "off", "target": "living room"}, {"action": "on", "target": "TV"}]
    )

    result = await HomeSkill(ctx).create_routine(name="movie time", steps=steps)

    assert "movie time" in result
    saved = ctx.settings.routines.items
    assert [r.name for r in saved] == ["movie time"]
    assert len(saved[0].steps) == 2


async def test_create_routine_replaces_a_same_named_one(ctx: NovaContext) -> None:
    make_home(ctx, *LOUNGE)
    skill = HomeSkill(ctx)
    await skill.create_routine(name="night", steps=json.dumps([{"action": "off", "target": "TV"}]))
    result = await skill.create_routine(
        name="Night", steps=json.dumps([{"action": "off", "target": "kitchen light"}])
    )
    assert "Updated" in result
    assert len(ctx.settings.routines.items) == 1  # not duplicated


async def test_create_routine_rejects_bad_json(ctx: NovaContext) -> None:
    make_home(ctx, *LOUNGE)
    with pytest.raises(IntegrationError, match="valid JSON"):
        await HomeSkill(ctx).create_routine(name="x", steps="turn everything off please")


async def test_create_routine_rejects_an_unknown_action(ctx: NovaContext) -> None:
    make_home(ctx, *LOUNGE)
    with pytest.raises(IntegrationError, match="action"):
        await HomeSkill(ctx).create_routine(
            name="x", steps=json.dumps([{"action": "dim", "target": "lounge lamp"}])
        )


# --------------------------------------------------------------------- run


async def test_run_routine_controls_a_room_and_a_device(ctx: NovaContext) -> None:
    _service, posts = make_home(ctx, *LOUNGE)
    skill = HomeSkill(ctx)
    await skill.create_routine(
        name="movie time",
        steps=json.dumps(
            [{"action": "off", "target": "living room"}, {"action": "on", "target": "TV"}]
        ),
    )
    posts.clear()

    result = await skill.run_routine(name="movie time")

    assert "Done" in result
    # Step 1: the whole living room off in one call…
    assert posts[0][0] == "/services/homeassistant/turn_off"
    assert set(posts[0][1]["entity_id"]) == {"light.lounge_1", "light.lounge_2", "media_player.tv"}
    # Step 2: the TV back on.
    assert posts[1][0] == "/services/homeassistant/turn_on"
    assert posts[1][1]["entity_id"] == "media_player.tv"


async def test_run_routine_applies_brightness(ctx: NovaContext) -> None:
    _service, posts = make_home(ctx, *LOUNGE)
    skill = HomeSkill(ctx)
    await skill.create_routine(
        name="dim",
        steps=json.dumps([{"action": "brightness", "target": "kitchen light", "value": "20"}]),
    )
    posts.clear()

    await skill.run_routine(name="dim")

    assert posts[0][0] == "/services/light/turn_on"
    assert posts[0][1]["brightness_pct"] == 20


async def test_run_routine_reports_a_step_it_could_not_run(ctx: NovaContext) -> None:
    make_home(ctx, *LOUNGE)
    skill = HomeSkill(ctx)
    await skill.create_routine(
        name="mixed",
        steps=json.dumps(
            [
                {"action": "on", "target": "kitchen light"},
                {"action": "on", "target": "nonexistent gadget"},
            ]
        ),
    )

    result = await skill.run_routine(name="mixed")

    assert "Partly ran" in result
    assert "nonexistent gadget" in result


async def test_run_routine_matches_the_name_case_insensitively(ctx: NovaContext) -> None:
    _service, posts = make_home(ctx, *LOUNGE)
    skill = HomeSkill(ctx)
    await skill.create_routine(
        name="Goodnight", steps=json.dumps([{"action": "off", "target": "kitchen light"}])
    )
    posts.clear()

    result = await skill.run_routine(name="goodnight")  # different case

    assert "Done" in result
    assert posts[0][0] == "/services/light/turn_off"  # kitchen light is a light
    assert posts[0][1]["entity_id"] == "light.kitchen"


async def test_unknown_routine_lists_what_is_saved(ctx: NovaContext) -> None:
    make_home(ctx, *LOUNGE)
    skill = HomeSkill(ctx)
    await skill.create_routine(
        name="movie time", steps=json.dumps([{"action": "off", "target": "TV"}])
    )

    with pytest.raises(IntegrationError, match="movie time"):
        await skill.run_routine(name="party mode")


# ----------------------------------------------------------- list / delete


async def test_list_and_delete_routines(ctx: NovaContext) -> None:
    make_home(ctx, *LOUNGE)
    skill = HomeSkill(ctx)
    await skill.create_routine(
        name="movie time",
        steps=json.dumps([{"action": "off", "target": "living room"}]),
    )

    listing = await skill.list_routines()
    assert "movie time" in listing
    assert "turn off living room" in listing

    await skill.delete_routine(name="movie time")
    assert ctx.settings.routines.items == []
