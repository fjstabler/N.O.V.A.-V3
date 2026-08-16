"""Calendar and reminders.

Dates are resolved locally by :func:`nova.integrations.calendar.parse_when`
before anything is stored, so the model never has to do arithmetic on
"next Tuesday" — it passes the phrase through and gets back a real timestamp.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Annotated

from ...integrations.calendar import Event, parse_duration, parse_when
from ...integrations.services import CalendarService
from ..base import Param, Skill, tool


class CalendarSkill(Skill):
    name = "calendar"
    description = "Read, create, move and delete calendar events; set reminders."
    category = "Calendar"
    prompt_hint = (
        "For anything about the calendar — what's on, free time, creating, moving or deleting "
        "events — call a calendar tool. Never answer from memory or a guess. For any question "
        "spanning more than one day ('this week', 'the next few days', 'which days am I "
        "working', 'am I free this weekend') call week_ahead, NOT agenda — agenda is a single "
        "day, and answering a week's question from one day's events (or from memory) is how you "
        "get it wrong. When the user has told you which events count as work (or anything "
        "similar), read the week with week_ahead and judge each day from the events it lists. A "
        "CalDAV account not being connected (see the environment below) does not mean the "
        "calendar is inaccessible: locally created events and reminders always work, so still "
        "check before saying you can't."
    )

    def is_available(self) -> tuple[bool, str]:
        if self.ctx.service("calendar") is None:
            return False, "calendar service is not running"
        return True, ""

    @property
    def calendar(self) -> CalendarService:
        return self.ctx.require("calendar", CalendarService)

    def context_lines(self) -> list[str]:
        calendar = self.ctx.service("calendar", CalendarService)
        if calendar is None:
            return []
        accounts = calendar.connected_accounts
        if accounts:
            return [f"Calendar accounts connected and syncing: {', '.join(accounts)}."]
        return [
            "No CalDAV account is connected right now — only locally created events "
            "and reminders are available."
        ]

    @tool("Get the events scheduled for a given day.")
    async def agenda(
        self,
        when: Annotated[
            str, Param("Day to look at", examples=("today", "tomorrow", "next Friday"))
        ] = "today",
    ) -> str:
        target, _ = parse_when(when)
        events = await self.calendar.agenda(target)
        label = _day_label(target)
        return CalendarService.describe_agenda(events, label)

    @tool(
        "List everything over a span of days, grouped by day — the tool for 'what's on this "
        "week', 'what am I doing over the next few days', 'which days am I working', 'am I free "
        "this weekend'. Understands 'this week' and 'next week' (Monday–Sunday); otherwise it "
        "starts from the given day. Use this, not agenda, for anything covering more than one day."
    )
    async def week_ahead(
        self,
        start: Annotated[
            str, Param("First day or span", examples=("this week", "next week", "today", "Monday"))
        ] = "this week",
        days: Annotated[int, Param("How many days to include (ignored for this/next week)")] = 7,
    ) -> str:
        lowered = start.strip().lower()
        if lowered in ("this week", "the week", "week"):
            target, days = _start_of_week(datetime.now()), 7
        elif lowered == "next week":
            target, days = _start_of_week(datetime.now()) + timedelta(days=7), 7
        else:
            target, _ = parse_when(start)
        days = max(1, min(days, 31))
        events = await self.calendar.agenda(target, days=days)
        return _describe_week(events, target, days)

    @tool("Get the next few upcoming events across all calendars.")
    async def upcoming(self, limit: Annotated[int, Param("How many events")] = 5) -> str:
        events = await self.calendar.upcoming(limit=max(1, min(limit, 20)))
        if not events:
            return "Nothing coming up."
        return "\n".join(e.describe() for e in events)

    @tool("Create a calendar event.", mutating=True)
    async def create_event(
        self,
        title: Annotated[str, Param("What the event is")],
        when: Annotated[
            str, Param("When it starts", examples=("tomorrow at 3pm", "Friday at half four"))
        ],
        duration_minutes: Annotated[int, Param("How long it lasts")] = 60,
        location: Annotated[str, Param("Where it takes place")] = "",
        notes: Annotated[str, Param("Extra detail")] = "",
    ) -> str:
        start, all_day = parse_when(when)
        minutes = duration_minutes or parse_duration(when)
        event = Event(
            summary=title,
            starts_at=start.timestamp(),
            ends_at=(start + timedelta(minutes=minutes)).timestamp(),
            location=location,
            description=notes,
            all_day=all_day,
        )
        await self.calendar.create(event)
        return f"Added '{title}' for {start.strftime('%A %d %B at %H:%M')}."

    @tool("Move an existing event to a new time.", mutating=True)
    async def move_event(
        self,
        title: Annotated[str, Param("Name of the event to move")],
        new_when: Annotated[str, Param("The new start time")],
    ) -> str:
        event = await self.calendar.find_one(title)
        start, _ = parse_when(new_when)
        moved = await self.calendar.move(event.uid, start)
        return f"Moved '{moved.summary}' to {start.strftime('%A %d %B at %H:%M')}."

    @tool("Delete a calendar event.", destructive=True)
    async def delete_event(
        self, title: Annotated[str, Param("Name of the event to delete")]
    ) -> str:
        event = await self.calendar.find_one(title)
        await self.calendar.delete(event.uid)
        return f"Deleted '{event.summary}'."

    @tool("Search the calendar for events matching some text.")
    async def search_events(self, text: Annotated[str, Param("Text to search for")]) -> str:
        events = await self.calendar.search(text)
        if not events:
            return f"Nothing matching '{text}'."
        return "\n".join(e.describe() for e in events[:10])

    @tool("Set a reminder at a given time.", mutating=True)
    async def set_reminder(
        self,
        what: Annotated[str, Param("What to be reminded about")],
        when: Annotated[str, Param("When", examples=("in 20 minutes", "tomorrow at 8am"))],
    ) -> str:
        start, _ = parse_when(when)
        event = Event(
            summary=what,
            starts_at=start.timestamp(),
            ends_at=start.timestamp() + 300,
            metadata={"kind": "reminder"},
        )
        await self.calendar.create(event)
        delta = start - datetime.now()
        if delta.total_seconds() < 3600:
            return f"I'll remind you in {max(1, int(delta.total_seconds() / 60))} minutes."
        return f"Reminder set for {start.strftime('%A at %H:%M')}."

    @tool("Find a free slot in the calendar on a given day.")
    async def find_free_time(
        self,
        when: Annotated[str, Param("Day to check")] = "today",
        duration_minutes: Annotated[int, Param("How much time is needed")] = 60,
    ) -> str:
        target, _ = parse_when(when)
        events = await self.calendar.agenda(target)
        busy = sorted(
            ((e.starts_at, e.ends_at) for e in events if not e.all_day), key=lambda pair: pair[0]
        )
        day_start = target.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
        day_end = target.replace(hour=21, minute=0, second=0, microsecond=0).timestamp()
        needed = duration_minutes * 60

        cursor = max(day_start, datetime.now().timestamp() if _is_today(target) else day_start)
        gaps: list[str] = []
        for start, end in busy:
            if start - cursor >= needed:
                gaps.append(_format_gap(cursor, start))
            cursor = max(cursor, end)
        if day_end - cursor >= needed:
            gaps.append(_format_gap(cursor, day_end))

        if not gaps:
            return f"No free {duration_minutes}-minute slot {_day_label(target)}."
        return f"Free {_day_label(target)}: " + "; ".join(gaps[:4]) + "."

    @tool("Force a calendar sync with the configured CalDAV accounts.", mutating=True)
    async def sync(self) -> str:
        count = await self.calendar.sync()
        return f"Synced {count} events." if count else "Calendar is up to date."


def _start_of_week(when: datetime) -> datetime:
    """Monday of the week containing ``when``, at midnight."""
    monday = when.date() - timedelta(days=when.weekday())
    return datetime.combine(monday, datetime.min.time())


def _describe_week(events: list[Event], start: datetime, days: int) -> str:
    """Events grouped under each day's heading, so a week reads at a glance.

    Days with nothing on are still listed as free — 'which days am I working'
    needs the empty days shown, not silently dropped, to be answerable.
    """
    span = start.strftime("%A %d %B")
    if not events:
        return f"Nothing scheduled in the {days} day(s) from {span}."

    by_day: dict[object, list[Event]] = defaultdict(list)
    for event in sorted(events, key=lambda e: e.starts_at):
        by_day[event.start.date()].append(event)

    lines: list[str] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).date()
        label = day.strftime("%A %d %B")
        day_events = by_day.get(day)
        if day_events:
            items = "; ".join(e.describe(include_date=False) for e in day_events)
            lines.append(f"{label}: {items}")
        else:
            lines.append(f"{label}: nothing scheduled")
    return "\n".join(lines)


def _day_label(target: datetime) -> str:
    today = datetime.now().date()
    if target.date() == today:
        return "today"
    if target.date() == today + timedelta(days=1):
        return "tomorrow"
    if target.date() == today - timedelta(days=1):
        return "yesterday"
    return f"on {target.strftime('%A %d %B')}"


def _is_today(target: datetime) -> bool:
    return target.date() == datetime.now().date()


def _format_gap(start: float, end: float) -> str:
    return (
        f"{datetime.fromtimestamp(start).strftime('%H:%M')}"
        f"–{datetime.fromtimestamp(end).strftime('%H:%M')}"
    )
