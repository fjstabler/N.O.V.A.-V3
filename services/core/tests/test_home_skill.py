"""HomeSkill: whether the model is actually told to use its tools.

Regression covered here: Home Assistant connected correctly (entities synced,
tools registered), but gpt-4o-mini answered "I cannot determine which lights
are on" without ever calling a tool — nothing in the prompt told it to prefer
a live check over guessing or over a stale, previously-recalled memory.
"""

from __future__ import annotations

import pytest

from nova.context import NovaContext
from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.integrations.services import HomeService
from nova.memory.models import Entity
from nova.memory.service import MemoryService
from nova.runtime.errors import IntegrationError
from nova.runtime.service import ServiceState
from nova.skills.builtin.home import HomeSkill
from nova.skills.registry import SkillRegistry


def make_running_home(ctx: NovaContext, **entities: HAEntity) -> HomeService:
    """A HomeService as it looks once Home Assistant has connected, without a
    real network call."""
    service = HomeService(ctx)
    ctx.services.register(service)
    client = HomeAssistantClient("http://ha.local:8123", "token")
    client._entities["light.kitchen"] = HAEntity(
        entity_id="light.kitchen",
        state="on",
        attributes={"friendly_name": "Kitchen Light", "area": "Kitchen"},
    )
    client._entities.update(entities)
    service.ha = client
    service._set_state(ServiceState.RUNNING)
    return service


async def make_running_memory(ctx: NovaContext) -> MemoryService:
    # Semantic recall would spawn a background model download; the alias
    # lookup only ever needs the lexical path, so keep it off in tests.
    ctx.store.patch({"memory": {"semantic_recall": False}}, persist=False)
    service = MemoryService(ctx)
    ctx.services.register(service)
    await service.start()
    return service


def test_prompt_hint_tells_the_model_to_check_live_state_not_guess(ctx: NovaContext) -> None:
    hint = HomeSkill(ctx).prompt_hint
    assert hint
    assert "tool" in hint.lower()


async def test_the_hint_reaches_the_prompt_once_home_is_connected(ctx: NovaContext) -> None:
    make_running_home(ctx)
    registry = SkillRegistry(ctx)
    await registry._install(HomeSkill)

    assert "home_whats_on" in registry._tools
    lines = registry.prompt_context()
    assert any("call a home tool" in line for line in lines)


async def test_no_hint_when_home_is_not_connected(ctx: NovaContext) -> None:
    """An unavailable skill must not leak its hint — that would tell the model
    to use tools it does not actually have."""
    registry = SkillRegistry(ctx)
    await registry._install(HomeSkill)

    assert "home_whats_on" not in registry._tools
    assert registry.prompt_context() == []


# --------------------------------------------------------------- name aliases


async def test_resolve_falls_back_to_a_taught_alias(ctx: NovaContext) -> None:
    """Regression: "the Govee lights" shares no words with the entity's real
    Home Assistant name ("fins tv backlights"), so the plain fuzzy match finds
    nothing — a previously taught alias has to be the escape hatch."""
    tv_lights = HAEntity(
        entity_id="light.fins_tv_backlights",
        state="off",
        attributes={"friendly_name": "fins tv backlights"},
    )
    make_running_home(ctx, **{tv_lights.entity_id: tv_lights})
    memory = await make_running_memory(ctx)
    await memory.upsert_entity(
        Entity(
            kind="ha_entity",
            name="fins tv backlights",
            aliases=["Govee lights"],
            attributes={"domain": "light", "area": ""},
        )
    )

    entity = await HomeSkill(ctx)._resolve("Govee lights")

    assert entity.entity_id == "light.fins_tv_backlights"


async def test_resolve_still_fails_when_no_alias_matches_either(ctx: NovaContext) -> None:
    make_running_home(ctx)
    await make_running_memory(ctx)

    with pytest.raises(IntegrationError, match="can't find anything called"):
        await HomeSkill(ctx)._resolve("the attic fan")


async def test_alias_lookup_respects_domain_filtering(ctx: NovaContext) -> None:
    plug = HAEntity(
        entity_id="switch.ipad_charger", state="off", attributes={"friendly_name": "Ipad Charger"}
    )
    make_running_home(ctx, **{plug.entity_id: plug})
    memory = await make_running_memory(ctx)
    await memory.upsert_entity(
        Entity(
            kind="ha_entity",
            name="Ipad Charger",
            aliases=["my plug"],
            attributes={"domain": "switch", "area": ""},
        )
    )
    skill = HomeSkill(ctx)

    with pytest.raises(IntegrationError, match="can't find anything called"):
        await skill._resolve("my plug", domain="light")

    entity = await skill._resolve("my plug", domain="switch")
    assert entity.entity_id == "switch.ipad_charger"


async def test_remember_device_alias_teaches_a_name_that_then_resolves(ctx: NovaContext) -> None:
    ceiling = HAEntity(
        entity_id="light.ceiling_light", state="off", attributes={"friendly_name": "Ceiling Light"}
    )
    make_running_home(ctx, **{ceiling.entity_id: ceiling})
    await make_running_memory(ctx)
    skill = HomeSkill(ctx)

    result = await skill.remember_device_alias(device="Ceiling Light", alias="the lamp")
    assert "Ceiling Light" in result

    entity = await skill._resolve("the lamp")
    assert entity.entity_id == "light.ceiling_light"


async def test_teaching_a_second_alias_does_not_erase_the_first(ctx: NovaContext) -> None:
    ceiling = HAEntity(
        entity_id="light.ceiling_light", state="off", attributes={"friendly_name": "Ceiling Light"}
    )
    make_running_home(ctx, **{ceiling.entity_id: ceiling})
    await make_running_memory(ctx)
    skill = HomeSkill(ctx)

    await skill.remember_device_alias(device="Ceiling Light", alias="the lamp")
    await skill.remember_device_alias(device="Ceiling Light", alias="the main light")

    assert (await skill._resolve("the lamp")).entity_id == "light.ceiling_light"
    assert (await skill._resolve("the main light")).entity_id == "light.ceiling_light"


async def test_remember_device_alias_rejects_an_unknown_device(ctx: NovaContext) -> None:
    make_running_home(ctx)
    await make_running_memory(ctx)

    with pytest.raises(IntegrationError, match="can't find anything called"):
        await HomeSkill(ctx).remember_device_alias(device="nonexistent gadget", alias="the thing")
