"""PersonWatchService: the auto person-alarm.

While armed, it checks the webcam on an interval and — when a person appears —
alerts through the presence system: spoken if you're home, pushed to your phone
if you're out. It uses on-device person detection (whole bodies, any angle), so
there are no faces to enrol; the trade-off is that it can't tell *who* the person
is, only that someone is there. That is exactly right for "tell me if anyone comes
into my room while I'm out": arm it as you leave an empty room, and the first
person in is the alert.

Edge-triggered on purpose: it fires once when the room goes from empty to
occupied, not continuously while someone is present, and the very first reading
after arming is a silent baseline so it never alerts on whoever armed it. Armed
and disarmed by voice, never running by default — the camera light coming on is
always a deliberate choice.
"""

from __future__ import annotations

import asyncio
import time

from .context import NovaContext
from .integrations.detection import looks_blank
from .integrations.local_camera import local_camera_pool
from .integrations.person_detect import PersonDetector
from .notifications import Level, Notification
from .runtime import Service, Topics
from .runtime.errors import MissingDependency, SkillError


class PersonWatchService(Service):
    """Owns the watch loop and the empty→occupied alerting."""

    name = "personwatch"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.detector = PersonDetector()
        self.armed = False
        self._task: asyncio.Task[None] | None = None
        self._last_alert_at = float("-inf")
        self._consecutive = 0
        #: Whether a person is currently considered present (so we alert on the
        #: transition into presence, not on every frame while they stay).
        self._present = False
        #: The first reading after arming is a silent baseline.
        self._baselined = False

    async def on_stop(self) -> None:
        await self.disarm()

    def describe(self) -> str:
        return "watching for people" if self.armed else "stood down"

    def _camera_index(self) -> int:
        override = self.ctx.settings.personwatch.camera_index
        return override if override >= 0 else self.ctx.settings.vision.camera_index

    # -------------------------------------------------------------- control

    async def arm(self) -> str:
        if not self.ctx.settings.vision.camera_enabled:
            raise SkillError("the camera is off — turn on Vision → Camera enabled first")
        try:
            await asyncio.to_thread(self.detector.ensure_ready)
        except MissingDependency as exc:
            raise SkillError(
                "the person detector isn't installed yet — run: "
                ".venv/bin/pip install -e 'services/core[person]'"
            ) from exc
        if self.armed:
            return "Already watching your room."
        self.armed = True
        self._consecutive = 0
        self._present = False
        self._baselined = False
        self._task = self.spawn(self._watch_loop(), name="personwatch")
        self.log.info("personwatch_armed", camera=self._camera_index())
        return "Watching your room — I'll let you know if someone comes in."

    async def disarm(self) -> str:
        if not self.armed:
            return "Already stood down."
        self.armed = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.log.info("personwatch_disarmed")
        return "Stood down — I've stopped watching."

    # ---------------------------------------------------------------- watch

    async def _watch_loop(self) -> None:
        index = self._camera_index()
        try:
            while self.armed:
                await self._check_once(index)
                await asyncio.sleep(self.ctx.settings.personwatch.poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A watch that stops watching without saying so is worse than one that
            # never started — but at minimum record it and stand down cleanly.
            self.log.exception("personwatch_loop_failed")
            self.armed = False

    async def _check_once(self, index: int) -> None:
        try:
            frame = await local_camera_pool.read_bgr(index)
        except Exception as exc:  # noqa: BLE001 - a camera glitch must not stop the watch
            self.log.warning("personwatch_frame_failed", error=str(exc))
            return
        if await asyncio.to_thread(looks_blank, frame):
            return  # a black frame is a camera problem, never an intruder
        self.detector.confidence = self.ctx.settings.vision.person_confidence
        try:
            confidences = await asyncio.to_thread(self.detector.detect, frame)
        except Exception as exc:  # noqa: BLE001 - a detection glitch must not stop the watch
            self.log.warning("personwatch_detect_failed", error=str(exc))
            return
        await self._register(len(confidences))

    async def _register(self, count: int) -> None:
        """Fold one reading into the edge-detection state, alerting on entry."""
        present = count > 0
        # The first reading after arming is a silent baseline, so arming a room
        # that already has someone in it (you, on your way out) does not alert.
        if not self._baselined:
            self._baselined = True
            self._present = present
            self._consecutive = 0
            return
        if not present:
            self._consecutive = 0
            self._present = False
            return

        self._consecutive += 1
        settings = self.ctx.settings.personwatch
        if self._present or self._consecutive < settings.confirm_frames:
            return
        now = time.monotonic()
        if now - self._last_alert_at < settings.alert_cooldown_seconds:
            return
        self._last_alert_at = now
        self._present = True
        await self._alert(count)

    async def _alert(self, count: int) -> None:
        title = "Someone's in your room"
        body = "a person just came in" if count == 1 else f"{count} people just came in"
        self.log.info("personwatch_alert", count=count)

        presence = self.ctx.service("presence")
        if presence is not None:
            # Spoken if you're home, pushed to your phone if you're away.
            await presence.reach_user(title, body, level="warning")
            return
        notifications = self.ctx.service("notifications")
        note = Notification(title=title, body=body, level=Level.WARNING, source=self.name)
        if notifications is not None:
            await notifications.raise_notification(note)
        else:
            self.bus.publish(Topics.NOTIFICATION, note.as_payload(), source=self.name)
