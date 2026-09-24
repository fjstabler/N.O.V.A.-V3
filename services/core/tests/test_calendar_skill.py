"""CalendarSkill: whether the model is told to use its tools and told the
truth about whether Apple/CalDAV sync is actually connected.

Regression covered here: the calendar service starts successfully even with
zero connected CalDAV accounts — a bad URL or password just falls back to
local-only storage rather than failing boot — so there was no live signal
telling the model apart "nothing is connected, only local events" from
"connected and synced". Combined with no prompt_hint at all (unlike memory
and, after an earlier fix, home), the model had nothing pushing it to call a
tool instead of just saying it couldn't access the calendar.
"""

from __future__ import annotations

from nova.context import NovaContext
from nova.integrations.services import CalendarService
from nova.skills.builtin.schedule import CalendarSkill
from nova.skills.registry import SkillRegistry


async def make_running_calendar(ctx: NovaContext) -> CalendarService:
    ctx.store.patch({"calendar": {"enabled": True}}, persist=False)
    service = CalendarService(ctx)
    ctx.services.register(service)
    await service.start()
    return service


def test_prompt_hint_tells_the_model_to_check_live_state_not_guess(ctx: NovaContext) -> None:
    hint = CalendarSkill(ctx).prompt_hint
    assert hint
    assert "tool" in hint.lower()


async def test_context_reports_no_account_connected(ctx: NovaContext) -> None:
    await make_running_calendar(ctx)
    lines = CalendarSkill(ctx).context_lines()
    assert any("No CalDAV account is connected" in line for line in lines)


async def test_context_reports_connected_accounts(ctx: NovaContext) -> None:
    service = await make_running_calendar(ctx)
    service._accounts["Apple"] = object()  # type: ignore[assignment]

    lines = CalendarSkill(ctx).context_lines()
    assert any("Apple" in line and "connected and syncing" in line for line in lines)


async def test_hint_and_connection_state_reach_the_prompt(ctx: NovaContext) -> None:
    await make_running_calendar(ctx)
    registry = SkillRegistry(ctx)
    await registry._install(CalendarSkill)

    assert "calendar_agenda" in registry._tools
    lines = registry.prompt_context()
    assert any("call a calendar tool" in line for line in lines)
    assert any("No CalDAV account is connected" in line for line in lines)
