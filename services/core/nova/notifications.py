"""Notification routing.

Every subsystem publishes to one topic; this service decides whether a panel
actually appears. The spec's rule — *never interrupt conversation* — is the
reason this exists as a service rather than a straight pass-through to the UI:
notifications raised while N.O.V.A. is listening, thinking or speaking are held
and released when it returns to idle.

Also handles do-not-disturb, quiet hours, de-duplication and the queue cap.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .context import NovaContext
from .runtime import NovaState, Service, Topics


class Level(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"
    PROMPT = "prompt"


#: Levels that ignore quiet hours and do-not-disturb.
_ALWAYS_SHOW = frozenset({Level.CRITICAL, Level.PROMPT})

#: Suppress an identical notification seen within this many seconds.
DEDUPE_WINDOW = 30.0


@dataclass(slots=True)
class Notification:
    title: str
    body: str = ""
    level: Level = Level.INFO
    icon: str = ""
    source: str = ""
    timeout: float = 8.0
    actions: list[dict[str, Any]] = field(default_factory=list)
    token: str = ""
    speak: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "level": self.level.value,
            "icon": self.icon or _icon_for(self.level),
            "source": self.source,
            "timeout": self.timeout,
            "actions": self.actions,
            "token": self.token,
            "createdAt": self.created_at,
        }

    def fingerprint(self) -> str:
        return f"{self.source}|{self.title}|{self.body}"


class NotificationService(Service):
    """Filters, queues and releases notifications."""

    name = "notifications"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self._deferred: deque[Notification] = deque(maxlen=20)
        self._recent: dict[str, float] = {}
        self._visible: dict[str, Notification] = {}

    async def on_start(self) -> None:
        self.bus.subscribe(Topics.NOTIFICATION, self._on_raised)
        self.bus.subscribe(Topics.STATE_CHANGED, self._on_state_changed)

    def describe(self) -> str:
        return f"{len(self._visible)} visible, {len(self._deferred)} deferred"

    # ----------------------------------------------------------------- intake

    async def _on_raised(self, event: Any) -> None:
        payload = event.payload
        # Re-published events carry an id already; those are ours, not new ones.
        if payload.get("_routed"):
            return
        notification = Notification(
            title=str(payload.get("title", "")),
            body=str(payload.get("body", "")),
            level=_coerce_level(payload.get("level")),
            icon=str(payload.get("icon", "")),
            source=str(payload.get("source", event.source or "")),
            timeout=float(payload.get("timeout", self.ctx.settings.notifications.default_timeout)),
            actions=list(payload.get("actions", [])),
            token=str(payload.get("token", "")),
            speak=bool(payload.get("speak", False)),
        )
        await self.raise_notification(notification)

    async def raise_notification(self, notification: Notification) -> bool:
        """Returns True if the panel was shown, False if suppressed or deferred."""
        settings = self.ctx.settings.notifications
        if not settings.enabled:
            return False

        if self._is_duplicate(notification):
            self.log.debug("notification_deduped", title=notification.title)
            return False

        if notification.level not in _ALWAYS_SHOW:
            if settings.do_not_disturb:
                return False
            if self._in_quiet_hours():
                self.log.debug("notification_quiet_hours", title=notification.title)
                return False

        if settings.defer_while_busy and self._busy() and notification.level is not Level.PROMPT:
            self._deferred.append(notification)
            self.log.debug(
                "notification_deferred", title=notification.title, queued=len(self._deferred)
            )
            return False

        self._emit(notification)
        return True

    def _emit(self, notification: Notification) -> None:
        settings = self.ctx.settings.notifications
        # Oldest panels retire first when the stack is full.
        while len(self._visible) >= settings.max_visible:
            oldest = min(self._visible.values(), key=lambda n: n.created_at)
            self.dismiss(oldest.id)

        self._visible[notification.id] = notification
        self._recent[notification.fingerprint()] = time.time()
        self.bus.publish(
            Topics.NOTIFICATION,
            {**notification.as_payload(), "_routed": True},
            source=self.name,
        )
        if notification.speak or (notification.level is Level.CRITICAL and settings.speak_critical):
            voice = self.ctx.service("voice")
            if voice is not None:
                self.spawn(voice.speak(f"{notification.title}. {notification.body}"))
        self.log.info("notification", title=notification.title, level=notification.level.value)

    def dismiss(self, notification_id: str) -> bool:
        if self._visible.pop(notification_id, None) is None:
            return False
        self.bus.publish(Topics.NOTIFICATION_DISMISS, {"id": notification_id}, source=self.name)
        return True

    def visible(self) -> list[dict[str, Any]]:
        return [n.as_payload() for n in self._visible.values()]

    # ------------------------------------------------------------------ gating

    def _busy(self) -> bool:
        return self.ctx.state.state in (NovaState.LISTENING, NovaState.THINKING, NovaState.SPEAKING)

    def _is_duplicate(self, notification: Notification) -> bool:
        fingerprint = notification.fingerprint()
        last = self._recent.get(fingerprint, 0.0)
        now = time.time()
        # Opportunistically expire old fingerprints so the map cannot grow forever.
        if len(self._recent) > 200:
            self._recent = {k: v for k, v in self._recent.items() if now - v < DEDUPE_WINDOW}
        return now - last < DEDUPE_WINDOW

    def _in_quiet_hours(self) -> bool:
        settings = self.ctx.settings.notifications
        start = _parse_hhmm(settings.quiet_hours_start)
        end = _parse_hhmm(settings.quiet_hours_end)
        if start is None or end is None:
            return False
        now = datetime.now().time()
        if start <= end:
            return start <= now < end
        # Window wraps midnight (e.g. 22:30 → 07:00).
        return now >= start or now < end

    async def _on_state_changed(self, event: Any) -> None:
        if event.payload.get("state") != NovaState.IDLE.value:
            return
        # Back to idle: release whatever was held during the conversation.
        while self._deferred:
            notification = self._deferred.popleft()
            # Anything older than five minutes is no longer news.
            if time.time() - notification.created_at > 300:
                continue
            self._emit(notification)


def notify(
    ctx: NovaContext,
    title: str,
    body: str = "",
    *,
    level: str = "info",
    icon: str = "",
    source: str = "",
    timeout: float = 8.0,
    speak: bool = False,
) -> None:
    """Convenience helper for raising a notification from anywhere."""
    ctx.bus.publish(
        Topics.NOTIFICATION,
        {
            "title": title,
            "body": body,
            "level": level,
            "icon": icon,
            "source": source,
            "timeout": timeout,
            "speak": speak,
        },
        source=source or "nova",
    )


def _coerce_level(value: Any) -> Level:
    try:
        return Level(str(value))
    except ValueError:
        return Level.INFO


def _icon_for(level: Level) -> str:
    return {
        Level.INFO: "info",
        Level.SUCCESS: "check",
        Level.WARNING: "alert",
        Level.CRITICAL: "alert",
        Level.PROMPT: "question",
    }[level]


def _parse_hhmm(raw: str):  # type: ignore[no-untyped-def]
    if not raw or ":" not in raw:
        return None
    try:
        hour, minute = raw.split(":", 1)
        return datetime.min.time().replace(hour=int(hour), minute=int(minute))
    except (ValueError, TypeError):
        return None
