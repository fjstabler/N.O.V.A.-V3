"""App boot order: a skill's is_available() check needs its backing service
already running.

Regression: `SecuritySkill.is_available()` checks whether `SecurityService`
is running, but `SkillRegistry.on_start()` calls `is_available()` for every
skill exactly once, synchronously, during its own start. `SecurityService`
was registered *after* `SkillRegistry` in `app.py`'s `_register_services()`,
and neither declares a `requires` on the other, so `ServiceManager`'s
topological sort — which falls back to registration order between services
with no dependency on each other — started `SkillRegistry` first every time.
`SecurityService` always looked not-yet-running to it, so
`security_arm_room_watch` and `security_learn_face` never made it into the
tools the model can call, for the entire life of the process — nothing
re-checks it after boot the way a home/homelab/calendar settings change does.
"Hey Nova, this is my face" and "watch my room" were never invoking
anything at all; the model was just talking, with no camera ever opened.
"""

from __future__ import annotations

from nova.app import NovaApplication
from nova.context import NovaContext
from nova.security.service import SecurityService
from nova.skills.registry import SkillRegistry


async def test_security_is_registered_before_skill_registry(store) -> None:
    """The concrete fix: guards app.py's registration order directly."""
    app = NovaApplication(store)
    order = app.ctx.services.resolve_order()
    assert order.index("security") < order.index("skills")


async def test_securitys_tools_are_available_when_it_starts_first(
    ctx: NovaContext,
) -> None:
    ctx.services.register(SecurityService(ctx))
    await ctx.services.get("security").start()

    registry = SkillRegistry(ctx)
    await registry.on_start()

    assert "security_learn_face" in registry._tools
    assert "security_arm_room_watch" in registry._tools


async def test_securitys_tools_are_missing_if_skill_registry_starts_first(
    ctx: NovaContext,
) -> None:
    """Same setup, reversed order — proves the previous test actually
    exercises the fix rather than passing for an unrelated reason."""
    ctx.services.register(SecurityService(ctx))
    registry = SkillRegistry(ctx)
    await registry.on_start()  # security is still CREATED, not running yet

    await ctx.services.get("security").start()

    assert "security_learn_face" not in registry._tools
    assert "security_arm_room_watch" not in registry._tools
