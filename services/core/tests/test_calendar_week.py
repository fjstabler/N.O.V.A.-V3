"""week_ahead: the multi-day view that makes "which days am I working" answerable.

The bug this covers: the only agenda tool looked at a single day, so a week-long
question had no tool behind it and got answered from a guess ("you're not working
this week") even with a calendar full of shifts. week_ahead lists every day in
the span — including the empty ones — so the model can judge each day from its
actual events.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from nova.context import NovaContext
from nova.integrations.calendar import Event
from nova.integrations.services import CalendarService
from nova.skills.builtin.schedule import CalendarSkill, _start_of_week


async def make_running_calendar(ctx: NovaContext) -> CalendarService:
    ctx.store.patch({"calendar": {"enabled": True}}, persist=False)
    service = CalendarService(ctx)
    ctx.services.register(service)
    await service.start()
    return service


def line_for(result: str, weekday_name: str) -> str:
    return next(line for line in result.splitlines() if line.startswith(weekday_name))


async def test_week_ahead_groups_shifts_by_day_and_shows_free_days(ctx: NovaContext) -> None:
    service = await make_running_calendar(ctx)
    monday = _start_of_week(datetime.now())

    # A work shift on Monday, Wednesday and Friday; Tuesday/Thursday off.
    for offset in (0, 2, 4):
        day = (monday + timedelta(days=offset)).replace(hour=9)
        await service.create(
            Event(
                summary="Work shift",
                starts_at=day.timestamp(),
                ends_at=day.replace(hour=17).timestamp(),
            )
        )

    result = await CalendarSkill(ctx).week_ahead(start="this week")

    # Three working days, each named, and the off days explicitly shown as free.
    assert result.count("Work shift") == 3
    assert "Work shift" in line_for(result, monday.strftime("%A"))
    assert "Work shift" in line_for(result, (monday + timedelta(days=4)).strftime("%A"))
    assert "nothing scheduled" in line_for(result, (monday + timedelta(days=1)).strftime("%A"))


async def test_week_ahead_reports_a_genuinely_empty_week(ctx: NovaContext) -> None:
    await make_running_calendar(ctx)
    result = await CalendarSkill(ctx).week_ahead(start="this week")
    assert "Nothing scheduled" in result


async def test_next_week_starts_seven_days_on(ctx: NovaContext) -> None:
    service = await make_running_calendar(ctx)
    next_monday = _start_of_week(datetime.now()) + timedelta(days=7)
    day = (next_monday + timedelta(days=1)).replace(hour=10)  # next Tuesday
    await service.create(
        Event(summary="Dentist", starts_at=day.timestamp(), ends_at=day.timestamp() + 1800)
    )

    this_week = await CalendarSkill(ctx).week_ahead(start="this week")
    next_week = await CalendarSkill(ctx).week_ahead(start="next week")

    assert "Dentist" not in this_week
    assert "Dentist" in next_week
