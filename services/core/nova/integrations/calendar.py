"""Calendar: a local event store with optional CalDAV synchronisation.

Local-first, like everything else. Events live in SQLite so the assistant can
answer "what's on today" instantly and offline; CalDAV sync is a background
reconciliation on top, not the source of truth. Events created by voice are
written locally first and pushed on the next sync, so scheduling never blocks on
a network round trip.

Natural-language dates are parsed here rather than handed to the model. "Tomorrow
at half four" is a solved problem that does not need a token round trip, and
resolving it locally means the model gets an unambiguous ISO timestamp.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..runtime.errors import IntegrationError, MissingDependency
from ..runtime.logging import get_logger

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    uid         TEXT PRIMARY KEY,
    account     TEXT NOT NULL DEFAULT '',
    calendar    TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    starts_at   REAL NOT NULL,
    ends_at     REAL NOT NULL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL,
    -- 'synced' | 'local-new' | 'local-updated' | 'local-deleted'
    sync_state  TEXT NOT NULL DEFAULT 'local-new',
    reminded    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(starts_at);
CREATE INDEX IF NOT EXISTS idx_events_sync  ON events(sync_state);
"""


@dataclass(slots=True)
class Event:
    summary: str
    starts_at: float
    ends_at: float
    uid: str = field(default_factory=lambda: f"nova-{uuid.uuid4().hex}")
    account: str = ""
    calendar: str = ""
    description: str = ""
    location: str = ""
    all_day: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    sync_state: str = "local-new"

    @property
    def start(self) -> datetime:
        return datetime.fromtimestamp(self.starts_at)

    @property
    def end(self) -> datetime:
        return datetime.fromtimestamp(self.ends_at)

    @property
    def duration_minutes(self) -> int:
        return max(0, int((self.ends_at - self.starts_at) / 60))

    def as_payload(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "description": self.description,
            "location": self.location,
            "startsAt": self.starts_at,
            "endsAt": self.ends_at,
            "allDay": self.all_day,
            "calendar": self.calendar,
            "account": self.account,
            "durationMinutes": self.duration_minutes,
        }

    def describe(self, *, include_date: bool = True) -> str:
        """A sentence suitable for reading aloud."""
        if self.all_day:
            when = self.start.strftime("%A %d %B") if include_date else "all day"
            return f"{self.summary}, {when}, all day"
        when = self.start.strftime("%A at %H:%M") if include_date else self.start.strftime("%H:%M")
        text = f"{self.summary} — {when}"
        if self.location:
            text += f", at {self.location}"
        return text


class CalendarStore:
    """SQLite persistence for events. Single-threaded, like the memory store."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def upsert(self, event: Event) -> Event:
        self._db.execute(
            """INSERT INTO events
               (uid, account, calendar, summary, description, location, starts_at, ends_at,
                all_day, metadata, updated_at, sync_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uid) DO UPDATE SET
                 summary=excluded.summary, description=excluded.description,
                 location=excluded.location, starts_at=excluded.starts_at,
                 ends_at=excluded.ends_at, all_day=excluded.all_day,
                 metadata=excluded.metadata, updated_at=excluded.updated_at,
                 sync_state=excluded.sync_state""",
            (
                event.uid,
                event.account,
                event.calendar,
                event.summary,
                event.description,
                event.location,
                event.starts_at,
                event.ends_at,
                int(event.all_day),
                json.dumps(event.metadata),
                time.time(),
                event.sync_state,
            ),
        )
        return event

    def get(self, uid: str) -> Event | None:
        row = self._db.execute("SELECT * FROM events WHERE uid = ?", (uid,)).fetchone()
        return _row_to_event(row) if row else None

    def range(self, start: float, end: float, *, limit: int = 100) -> list[Event]:
        rows = self._db.execute(
            """SELECT * FROM events
               WHERE ends_at >= ? AND starts_at <= ? AND sync_state != 'local-deleted'
               ORDER BY starts_at LIMIT ?""",
            (start, end, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def search(self, text: str, *, limit: int = 20) -> list[Event]:
        pattern = f"%{text.lower()}%"
        rows = self._db.execute(
            """SELECT * FROM events
               WHERE (LOWER(summary) LIKE ? OR LOWER(description) LIKE ? OR LOWER(location) LIKE ?)
                 AND sync_state != 'local-deleted'
               ORDER BY ABS(starts_at - ?) LIMIT ?""",
            (pattern, pattern, pattern, time.time(), limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def mark_deleted(self, uid: str) -> bool:
        cursor = self._db.execute(
            "UPDATE events SET sync_state = 'local-deleted', updated_at = ? WHERE uid = ?",
            (time.time(), uid),
        )
        return cursor.rowcount > 0

    def purge(self, uid: str) -> None:
        self._db.execute("DELETE FROM events WHERE uid = ?", (uid,))

    def pending_sync(self) -> list[Event]:
        rows = self._db.execute("SELECT * FROM events WHERE sync_state LIKE 'local-%'").fetchall()
        return [_row_to_event(r) for r in rows]

    def replace_synced(self, account: str, events: list[Event]) -> int:
        """Swap in a fresh server snapshot, preserving unsynced local edits."""
        local_uids = {e.uid for e in self.pending_sync()}
        self._db.execute(
            "DELETE FROM events WHERE account = ? AND sync_state = 'synced'", (account,)
        )
        written = 0
        for event in events:
            if event.uid in local_uids:
                continue  # a local edit wins until it has been pushed
            event.sync_state = "synced"
            self.upsert(event)
            written += 1
        return written

    def due_reminders(self, lead_seconds: float) -> list[Event]:
        """Events starting within the lead window that have not been announced."""
        now = time.time()
        rows = self._db.execute(
            """SELECT * FROM events
               WHERE reminded = 0 AND all_day = 0
                 AND starts_at BETWEEN ? AND ?
                 AND sync_state != 'local-deleted'
               ORDER BY starts_at""",
            (now, now + lead_seconds),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def mark_reminded(self, uid: str) -> None:
        self._db.execute("UPDATE events SET reminded = 1 WHERE uid = ?", (uid,))

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def close(self) -> None:
        self._db.close()


class CalDAVAccount:
    """One CalDAV server connection."""

    def __init__(
        self, name: str, url: str, username: str, password: str, *, read_only: bool = False
    ) -> None:
        self.name = name
        self.url = url
        self.username = username
        self.password = password
        self.read_only = read_only
        self._principal: Any = None

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        try:
            import caldav
        except ImportError as exc:
            raise MissingDependency("calendar", "caldav", "home") from exc
        try:
            client = caldav.DAVClient(url=self.url, username=self.username, password=self.password)
            self._principal = client.principal()
        except Exception as exc:
            raise IntegrationError("calendar", f"{self.name}: {exc}") from exc
        log.info("caldav_connected", account=self.name)

    async def fetch(self, days_back: int = 7, days_ahead: int = 90) -> list[Event]:
        return await asyncio.to_thread(self._fetch_sync, days_back, days_ahead)

    def _fetch_sync(self, days_back: int, days_ahead: int) -> list[Event]:
        if self._principal is None:
            return []
        start = datetime.now() - timedelta(days=days_back)
        end = datetime.now() + timedelta(days=days_ahead)
        events: list[Event] = []
        for calendar in self._principal.calendars():
            try:
                results = calendar.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:  # noqa: BLE001 - one bad calendar shouldn't kill the sync
                log.warning("caldav_calendar_failed", calendar=str(calendar), error=str(exc)[:120])
                continue
            for item in results:
                event = _from_ical(item, self.name, str(getattr(calendar, "name", "")))
                if event is not None:
                    events.append(event)
        return events

    async def push(self, event: Event) -> bool:
        if self.read_only:
            return False
        return await asyncio.to_thread(self._push_sync, event)

    def _push_sync(self, event: Event) -> bool:
        if self._principal is None:
            return False
        try:
            calendars = self._principal.calendars()
            if not calendars:
                return False
            target = calendars[0]
            target.save_event(
                dtstart=event.start,
                dtend=event.end,
                summary=event.summary,
                description=event.description,
                location=event.location,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("caldav_push_failed", uid=event.uid, error=str(exc)[:160])
            return False

    async def delete(self, uid: str) -> bool:
        if self.read_only:
            return False
        return await asyncio.to_thread(self._delete_sync, uid)

    def _delete_sync(self, uid: str) -> bool:
        if self._principal is None:
            return False
        for calendar in self._principal.calendars():
            with contextlib.suppress(Exception):
                calendar.event_by_uid(uid).delete()
                return True
        return False


# ------------------------------------------------------------ natural language

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_TIME_WORDS = {
    "midnight": (0, 0),
    "noon": (12, 0),
    "midday": (12, 0),
    "morning": (9, 0),
    "lunchtime": (12, 30),
    "afternoon": (14, 0),
    "evening": (19, 0),
    "tonight": (20, 0),
    "night": (21, 0),
}
#: Transcripts mix digits and words freely — "half past 4" and "half past four"
#: are both common, so every clock pattern accepts either form.
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_HOUR = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"

_DURATION = re.compile(r"(?:for\s+)?(\d+)\s*(minute|min|hour|hr|h|m)s?\b", re.I)
_CLOCK = re.compile(
    rf"\b(?:at\s+)?{_HOUR}(?::(\d{{2}}))?\s*(am|pm)?\b(?!\s*(?:minute|min|hour|hr|second))", re.I
)
_HALF_PAST = re.compile(rf"\bhalf\s+(?:past\s+)?{_HOUR}\b", re.I)
_QUARTER = re.compile(rf"\b(quarter)\s+(past|to)\s+{_HOUR}\b", re.I)


def _hour_value(token: str) -> int:
    """Read an hour written as a digit or a word."""
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token, 0)


def parse_when(text: str, *, now: datetime | None = None) -> tuple[datetime, bool]:
    """Resolve a spoken time expression to ``(datetime, is_all_day)``.

    Handles the phrasings that actually come out of a voice transcript: "tomorrow
    at 3", "next Tuesday", "half four", "in 20 minutes", "this evening".
    """
    now = now or datetime.now()
    lowered = text.lower().strip()
    target_date = now.date()
    all_day = True
    explicit_date = False

    if match := re.search(r"\bin\s+(\d+)\s*(minute|min|hour|hr|day|week)s?\b", lowered):
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "minute": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
        }[unit]
        return now + delta, False

    if "day after tomorrow" in lowered:
        target_date, explicit_date = now.date() + timedelta(days=2), True
    elif "tomorrow" in lowered:
        target_date, explicit_date = now.date() + timedelta(days=1), True
    elif "yesterday" in lowered:
        target_date, explicit_date = now.date() - timedelta(days=1), True
    elif "today" in lowered or "tonight" in lowered:
        target_date, explicit_date = now.date(), True
    else:
        for name, index in _WEEKDAYS.items():
            if name in lowered:
                ahead = (index - now.weekday()) % 7
                if ahead == 0 or "next" in lowered:
                    ahead = ahead or 7
                    if "next" in lowered and ahead < 7:
                        ahead += 7
                target_date, explicit_date = now.date() + timedelta(days=ahead), True
                break
        else:
            if iso := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", lowered):
                target_date = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
                explicit_date = True
            elif dmy := re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lowered):
                day, month = int(dmy.group(1)), int(dmy.group(2))
                year = int(dmy.group(3) or now.year)
                year += 2000 if year < 100 else 0
                with contextlib.suppress(ValueError):
                    target_date, explicit_date = date(year, month, day), True

    hour, minute = _parse_clock(lowered)
    if hour is not None:
        all_day = False
        result = datetime.combine(target_date, datetime.min.time()).replace(
            hour=hour, minute=minute
        )
        # "at 8" when it is already 9pm means tomorrow, unless a date was given.
        if not explicit_date and result < now:
            result += timedelta(days=1)
        return result, False

    if explicit_date:
        return datetime.combine(target_date, datetime.min.time()).replace(hour=9), all_day
    return now, False


def _parse_clock(text: str) -> tuple[int | None, int]:
    for word, (hour, minute) in _TIME_WORDS.items():
        if word in text:
            return hour, minute

    if match := _QUARTER.search(text):
        hour = _hour_value(match.group(3))
        minute = 15 if match.group(2).lower() == "past" else 45
        if match.group(2).lower() == "to":
            hour -= 1
        return _to_24h(hour, text), minute

    if match := _HALF_PAST.search(text):
        return _to_24h(_hour_value(match.group(1)), text), 30

    if match := _CLOCK.search(text):
        hour = _hour_value(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem:
            hour = _to_24h(hour, text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None, 0


def _to_24h(hour: int, text: str) -> int:
    """Assume the sensible half of the clock when nobody said am or pm."""
    if "pm" in text or "evening" in text or "tonight" in text:
        return hour + 12 if hour < 12 else hour
    if "am" in text or "morning" in text:
        return hour
    # 1–7 without a qualifier almost always means the afternoon.
    return hour + 12 if 1 <= hour <= 7 else hour


def parse_duration(text: str, *, default_minutes: int = 60) -> int:
    if match := _DURATION.search(text):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        return amount * 60 if unit in ("hour", "hr", "h") else amount
    return default_minutes


def day_bounds(when: datetime) -> tuple[float, float]:
    start = datetime.combine(when.date(), datetime.min.time())
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


# ------------------------------------------------------------------- mapping


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        uid=row["uid"],
        account=row["account"],
        calendar=row["calendar"],
        summary=row["summary"],
        description=row["description"],
        location=row["location"],
        starts_at=float(row["starts_at"]),
        ends_at=float(row["ends_at"]),
        all_day=bool(row["all_day"]),
        metadata=_loads(row["metadata"]),
        sync_state=row["sync_state"],
    )


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _from_ical(item: Any, account: str, calendar_name: str) -> Event | None:
    """Convert a caldav event object into our model."""
    try:
        component = item.icalendar_component
        start = component.get("dtstart")
        end = component.get("dtend")
        if start is None:
            return None
        start_value = start.dt
        all_day = not isinstance(start_value, datetime)
        start_dt = datetime.combine(start_value, datetime.min.time()) if all_day else start_value
        if end is not None:
            end_value = end.dt
            end_dt = datetime.combine(end_value, datetime.min.time()) if all_day else end_value
        else:
            end_dt = start_dt + timedelta(hours=1)
        return Event(
            uid=str(component.get("uid", uuid.uuid4().hex)),
            account=account,
            calendar=calendar_name,
            summary=str(component.get("summary", "(untitled)")),
            description=str(component.get("description", "")),
            location=str(component.get("location", "")),
            starts_at=start_dt.timestamp(),
            ends_at=end_dt.timestamp(),
            all_day=all_day,
            sync_state="synced",
        )
    except Exception as exc:  # noqa: BLE001 - malformed ICS is common in the wild
        log.debug("ical_parse_failed", error=str(exc)[:120])
        return None
