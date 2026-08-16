"""FrigateService: turn Frigate's person detections into alerts that reach you.

Every Home Assistant state change already arrives here as a HOME_EVENT. When one
is a Frigate object sensor turning on — a person stepping into a camera's view —
this routes an alert through the presence system: spoken if you're home, pushed
to your phone if you're out. That is the reliable "camera alert" the webcam
face-watch could never quite be, because the detection is Frigate's, done on its
own models, not a face the webcam has to catch head-on.

Cheap and always-registered: it no-ops unless Frigate is enabled, and every
channel it uses degrades on its own.
"""

from __future__ import annotations

import time
from typing import Any

from .context import NovaContext
from .integrations.frigate import camera_name, is_detection_event
from .notifications import Level, Notification
from .runtime import Service, Topics


class FrigateService(Service):
    """Watches HOME_EVENT for Frigate detections and alerts through presence."""

    name = "frigate"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        #: Last alert time per camera sensor, for the cooldown. -inf = never.
        self._last_alert: dict[str, float] = {}

    async def on_start(self) -> None:
        self.bus.subscribe(Topics.HOME_EVENT, self._on_home_event)

    def describe(self) -> str:
        settings = self.ctx.settings.frigate
        if not settings.enabled:
            return "disabled"
        return f"watching for {settings.object_type} detections"

    async def _on_home_event(self, event: Any) -> None:
        settings = self.ctx.settings.frigate
        if not settings.enabled or not settings.alert_on_detection:
            return
        payload = event.payload if hasattr(event, "payload") else {}
        if not is_detection_event(payload, settings.object_type):
            return

        entity_id = str(payload.get("entityId", ""))
        camera = camera_name(entity_id, settings.object_type)
        if settings.cameras and not _camera_allowed(camera, settings.cameras):
            return

        now = time.monotonic()
        if now - self._last_alert.get(entity_id, float("-inf")) < settings.alert_cooldown_seconds:
            return
        self._last_alert[entity_id] = now
        await self._alert(camera, settings.object_type)

    async def _alert(self, camera: str, object_type: str) -> None:
        title = f"{camera.title()} camera" if camera else "Camera alert"
        body = f"{object_type} detected"
        self.log.info("frigate_alert", camera=camera, object=object_type)

        presence = self.ctx.service("presence")
        if presence is not None:
            # Spoken if you're home, pushed to your phone if you're away.
            await presence.reach_user(title, body, level="warning")
            return
        # No presence service: at least raise a panel.
        notifications = self.ctx.service("notifications")
        note = Notification(title=title, body=body, level=Level.WARNING, source=self.name)
        if notifications is not None:
            await notifications.raise_notification(note)
        else:
            self.bus.publish(Topics.NOTIFICATION, note.as_payload(), source=self.name)


def _camera_allowed(camera: str, allowed: list[str]) -> bool:
    needle = camera.strip().lower()
    return any(
        a.strip().lower() in needle or needle in a.strip().lower() for a in allowed if a.strip()
    )
