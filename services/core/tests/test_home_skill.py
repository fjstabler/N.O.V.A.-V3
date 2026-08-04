"""HomeSkill: whether the model is actually told to use its tools.

Regression covered here: Home Assistant connected correctly (entities synced,
tools registered), but gpt-4o-mini answered "I cannot determine which lights
are on" without ever calling a tool — nothing in the prompt told it to prefer
a live check over guessing or over a stale, previously-recalled memory.
"""

from __future__ import annotations

from nova.context import NovaContext
from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.integrations.services import HomeService
from nova.runtime.service import ServiceState
from nova.skills.builtin.home import HomeSkill
from nova.skills.registry import SkillRegistry


def make_running_home(ctx: NovaContext) -> HomeService:
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
    service.ha = client
    service._set_state(ServiceState.RUNNING)
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
