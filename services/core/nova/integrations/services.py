"""Long-lived integration services: home, home lab, calendar.

Each owns its clients and background loops, publishes events onto the bus, and
exposes a small API for the skills that sit on top. They are all optional: a
service whose integration is not configured raises
:class:`DegradedCapability` at start, which marks it degraded and hides the
matching skill instead of failing the boot.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from ..context import NovaContext
from ..runtime import Service, Topics
from ..runtime.errors import DegradedCapability, IntegrationError
from .calendar import CalDAVAccount, CalendarStore, Event, day_bounds
from .homeassistant import HAEntity, HomeAssistantClient
from .homelab.adapters import build_adapter
from .homelab.base import ServiceAdapter, ServiceStatus
from .mqtt import MQTTClient

#: State transitions worth announcing without being asked.
_NOTABLE_DOMAINS = frozenset({"binary_sensor", "lock", "cover", "alarm_control_panel"})


class HomeService(Service):
    """Home Assistant and MQTT."""

    name = "home"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.ha: HomeAssistantClient | None = None
        self.mqtt: MQTTClient | None = None

    async def on_start(self) -> None:
        settings = self.ctx.settings
        started: list[str] = []

        if settings.home_assistant.enabled:
            client = HomeAssistantClient(
                settings.home_assistant.url,
                settings.home_assistant.token,
                verify_ssl=settings.home_assistant.verify_ssl,
                exposed_domains=tuple(settings.home_assistant.exposed_domains),
                on_state_change=self._on_ha_state,
            )
            try:
                await client.connect()
                self.ha = client
                started.append(f"HA ({client.entity_count} entities)")
                self.spawn(self._index_entities(), name="home-entity-index")
            except IntegrationError as exc:
                self.log.warning("home_assistant_unavailable", error=exc.message)

        if settings.mqtt.enabled:
            client = MQTTClient(
                host=settings.mqtt.host,
                port=settings.mqtt.port,
                username=settings.mqtt.username,
                password=settings.mqtt.password,
                tls=settings.mqtt.tls,
                client_id=settings.mqtt.client_id,
                on_message=self._on_mqtt_message,
            )
            try:
                await client.connect()
                for topic in settings.mqtt.subscribe:
                    await client.subscribe(topic)
                self.mqtt = client
                started.append(f"MQTT ({settings.mqtt.host})")
            except (IntegrationError, DegradedCapability) as exc:
                self.log.warning("mqtt_unavailable", error=str(exc))

        if not started:
            raise DegradedCapability("home", "no home integration is configured and reachable")
        self.log.info("home_ready", integrations=started)

    async def on_stop(self) -> None:
        if self.ha is not None:
            await self.ha.close()
        if self.mqtt is not None:
            await self.mqtt.close()

    def describe(self) -> str:
        parts = []
        if self.ha is not None:
            parts.append(f"HA:{self.ha.entity_count}")
        if self.mqtt is not None:
            parts.append(f"MQTT:{len(self.mqtt.retained)}")
        return " · ".join(parts)

    async def _index_entities(self) -> None:
        """Teach memory the names of every device, so recall can resolve them."""
        memory = self.ctx.service("memory")
        if memory is None or self.ha is None:
            return
        from ..memory.models import Entity

        for entity in self.ha.entities():
            await memory.upsert_entity(
                Entity(
                    kind="ha_entity",
                    name=entity.friendly_name,
                    aliases=[entity.entity_id],
                    attributes={"domain": entity.domain, "area": entity.area},
                )
            )
        self.log.info("entities_indexed", count=self.ha.entity_count)

    def _on_ha_state(self, entity: HAEntity, previous: HAEntity) -> None:
        self.bus.publish(
            Topics.HOME_EVENT,
            {
                "entityId": entity.entity_id,
                "name": entity.friendly_name,
                "state": entity.state,
                "previous": previous.state,
                "domain": entity.domain,
            },
            source=self.name,
        )
        # Doors, locks and alarms are the ones worth interrupting for.
        if (
            entity.domain in _NOTABLE_DOMAINS
            and entity.attributes.get("device_class")
            in ("door", "window", "garage_door", "motion", "safety", "smoke", "gas")
            and entity.state in ("on", "open", "unlocked", "detected")
        ):
            self.bus.publish(
                Topics.NOTIFICATION,
                {
                    "level": "info",
                    "title": entity.friendly_name,
                    "body": f"{entity.attributes.get('device_class', 'sensor')} {entity.state}",
                    "icon": "home",
                    "source": "home",
                },
                source=self.name,
            )

    def _on_mqtt_message(self, topic: str, payload: str) -> None:
        self.bus.publish(
            Topics.HOME_EVENT, {"topic": topic, "payload": payload[:512]}, source=self.name
        )

    def require_ha(self) -> HomeAssistantClient:
        if self.ha is None:
            raise IntegrationError(
                "home assistant", "not connected — check the URL and token in settings"
            )
        return self.ha

    def require_mqtt(self) -> MQTTClient:
        if self.mqtt is None:
            raise IntegrationError("mqtt", "not connected — check the broker settings")
        return self.mqtt


class HomeLabService(Service):
    """Polls self-hosted services and remembers their last known state."""

    name = "homelab"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.adapters: dict[str, ServiceAdapter] = {}
        self.statuses: dict[str, ServiceStatus] = {}

    async def on_start(self) -> None:
        settings = self.ctx.settings.homelab
        if not settings.enabled or not settings.services:
            raise DegradedCapability("homelab", "no services configured")

        for config in settings.services:
            if not config.enabled or not config.url:
                continue
            adapter = build_adapter(config)
            self.adapters[adapter.name.lower()] = adapter

        if not self.adapters:
            raise DegradedCapability("homelab", "no enabled services with a URL")

        await self.refresh()
        self.spawn(self._poll_loop(), name="homelab-poll")
        self.log.info("homelab_ready", services=sorted(self.adapters))

    async def on_stop(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()

    def describe(self) -> str:
        online = sum(1 for s in self.statuses.values() if s.online)
        return f"{online}/{len(self.adapters)} online"

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ctx.settings.homelab.poll_interval_seconds)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("homelab_poll_failed")

    async def refresh(self) -> list[ServiceStatus]:
        """Check every service concurrently and notify on transitions."""
        results = await asyncio.gather(
            *(adapter.status() for adapter in self.adapters.values()), return_exceptions=True
        )
        statuses: list[ServiceStatus] = []
        for adapter, result in zip(self.adapters.values(), results, strict=True):
            if isinstance(result, BaseException):
                status = ServiceStatus(adapter.name, adapter.kind, False, str(result)[:120])
            else:
                status = result
            previous = self.statuses.get(status.name.lower())
            self.statuses[status.name.lower()] = status
            statuses.append(status)

            # Edge-triggered: only a change in reachability is worth a panel.
            if previous is not None and previous.online != status.online:
                self.bus.publish(
                    Topics.NOTIFICATION,
                    {
                        "level": "warning" if not status.online else "success",
                        "title": (
                            f"{status.name} {'is back' if status.online else 'went offline'}"
                        ),
                        "body": status.detail,
                        "icon": "server",
                        "source": "homelab",
                        "timeout": 10.0,
                    },
                    source=self.name,
                )
                self.log.info("homelab_transition", service=status.name, online=status.online)
        return statuses

    def find(self, name: str) -> ServiceAdapter:
        needle = name.strip().lower()
        if needle in self.adapters:
            return self.adapters[needle]
        matches = [a for key, a in self.adapters.items() if needle in key or needle in a.kind]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise IntegrationError("homelab", f"no service called '{name}'")
        raise IntegrationError("homelab", f"'{name}' matches {', '.join(a.name for a in matches)}")

    def snapshot(self) -> list[dict[str, Any]]:
        return [s.as_payload() for s in self.statuses.values()]


class CalendarService(Service):
    """Local event store plus CalDAV synchronisation and reminders."""

    name = "calendar"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self._store: CalendarStore | None = None
        self._accounts: dict[str, CalDAVAccount] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nova-calendar")

    async def on_start(self) -> None:
        settings = self.ctx.settings.calendar
        if not settings.enabled:
            raise DegradedCapability("calendar", "disabled in settings")

        self._store = await self._run(lambda: CalendarStore(self.ctx.paths.calendar_db))

        for config in settings.accounts:
            if not config.url:
                continue
            account = CalDAVAccount(
                config.name,
                config.url,
                config.username,
                config.password,
                read_only=config.read_only,
            )
            try:
                await account.connect()
                self._accounts[config.name] = account
            except (IntegrationError, DegradedCapability) as exc:
                self.log.warning("caldav_unavailable", account=config.name, error=str(exc))

        self.spawn(self._sync_loop(), name="calendar-sync")
        self.spawn(self._reminder_loop(), name="calendar-reminders")
        count = await self._run(self.store.count)
        self.log.info("calendar_ready", accounts=len(self._accounts), events=count)

    async def on_stop(self) -> None:
        if self._store is not None:
            await self._run(self._store.close)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def describe(self) -> str:
        return f"{len(self._accounts)} account(s)" if self._accounts else "local only"

    @property
    def store(self) -> CalendarStore:
        if self._store is None:
            raise IntegrationError("calendar", "store not started")
        return self._store

    async def _run(self, fn: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    # ------------------------------------------------------------------- sync

    async def _sync_loop(self) -> None:
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("calendar_sync_failed")
            await asyncio.sleep(self.ctx.settings.calendar.sync_interval_minutes * 60)

    async def sync(self) -> int:
        """Push local changes, then pull a fresh snapshot per account."""
        if not self._accounts:
            return 0
        total = 0
        pending = await self._run(self.store.pending_sync)
        for event in pending:
            account = self._accounts.get(event.account) or next(iter(self._accounts.values()), None)
            if account is None:
                continue
            if event.sync_state == "local-deleted":
                if await account.delete(event.uid):
                    await self._run(lambda e=event: self.store.purge(e.uid))
            elif await account.push(event):
                event.sync_state = "synced"
                await self._run(lambda e=event: self.store.upsert(e))

        for name, account in self._accounts.items():
            try:
                events = await account.fetch()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("calendar_fetch_failed", account=name, error=str(exc)[:160])
                continue
            written = await self._run(lambda n=name, ev=events: self.store.replace_synced(n, ev))
            total += written
        self.log.info("calendar_synced", events=total)
        return total

    # --------------------------------------------------------------- reminders

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            lead = self.ctx.settings.calendar.reminder_lead_minutes * 60
            if lead <= 0:
                continue
            try:
                due = await self._run(lambda seconds=lead: self.store.due_reminders(seconds))
            except Exception:  # noqa: BLE001
                continue
            for event in due:
                minutes = max(0, int((event.starts_at - time.time()) / 60))
                self.bus.publish(
                    Topics.CALENDAR_REMINDER,
                    {"event": event.as_payload(), "minutesUntil": minutes},
                    source=self.name,
                )
                self.bus.publish(
                    Topics.NOTIFICATION,
                    {
                        "level": "info",
                        "title": event.summary,
                        "body": (
                            f"starts in {minutes} minute{'s' if minutes != 1 else ''}"
                            + (f" · {event.location}" if event.location else "")
                        ),
                        "icon": "calendar",
                        "source": "calendar",
                        "timeout": 15.0,
                        "speak": True,
                    },
                    source=self.name,
                )
                await self._run(lambda e=event: self.store.mark_reminded(e.uid))

    # -------------------------------------------------------------- operations

    async def agenda(self, when: datetime | None = None, *, days: int = 1) -> list[Event]:
        when = when or datetime.now()
        start, _ = day_bounds(when)
        end = start + days * 86400
        return await self._run(lambda: self.store.range(start, end))

    async def upcoming(self, *, limit: int = 10, days: int = 14) -> list[Event]:
        now = time.time()
        events = await self._run(lambda: self.store.range(now, now + days * 86400, limit=limit))
        return events

    async def create(self, event: Event) -> Event:
        if not event.account and self._accounts:
            event.account = self.ctx.settings.calendar.default_account or next(iter(self._accounts))
        stored = await self._run(lambda: self.store.upsert(event))
        # Push immediately so the phone shows it before the next sync tick.
        self.spawn(self._push_one(stored), name="calendar-push")
        return stored

    async def _push_one(self, event: Event) -> None:
        account = self._accounts.get(event.account)
        if account is not None and await account.push(event):
            event.sync_state = "synced"
            await self._run(lambda: self.store.upsert(event))

    async def move(self, uid: str, new_start: datetime) -> Event:
        event = await self._run(lambda: self.store.get(uid))
        if event is None:
            raise IntegrationError("calendar", "no such event")
        duration = event.ends_at - event.starts_at
        event.starts_at = new_start.timestamp()
        event.ends_at = event.starts_at + duration
        event.sync_state = "local-updated"
        return await self._run(lambda: self.store.upsert(event))

    async def delete(self, uid: str) -> bool:
        return await self._run(lambda: self.store.mark_deleted(uid))

    async def search(self, text: str) -> list[Event]:
        return await self._run(lambda: self.store.search(text))

    async def find_one(self, text: str) -> Event:
        matches = await self.search(text)
        if not matches:
            raise IntegrationError("calendar", f"I can't find an event matching '{text}'")
        return matches[0]

    @staticmethod
    def describe_agenda(events: list[Event], label: str = "today") -> str:
        if not events:
            return f"Nothing scheduled {label}."
        if len(events) == 1:
            return f"One thing {label}: {events[0].describe(include_date=False)}."
        lines = "; ".join(e.describe(include_date=False) for e in events[:6])
        more = f", and {len(events) - 6} more" if len(events) > 6 else ""
        return f"{len(events)} things {label}: {lines}{more}."


def next_occurrence(weekday: int, *, now: datetime | None = None) -> datetime:
    """Next date falling on ``weekday`` (0 = Monday), never today."""
    now = now or datetime.now()
    ahead = (weekday - now.weekday()) % 7 or 7
    return datetime.combine((now + timedelta(days=ahead)).date(), datetime.min.time())
