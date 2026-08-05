"""SecurityService: room-watch.

Armed and disarmed by voice, never left running by default (see
`config.schema.SecuritySettings` for why). While armed, samples the
configured camera on an interval, and for anyone whose face does not match
an enrolled one, for `confirm_frames` consecutive samples in a row: speaks a
warning immediately, raises a notification, and pushes to the phone via
ntfy — each independent of the others, so one channel failing (no ntfy
topic set, no voice service, notifications disabled) does not take the
others down with it.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any
from urllib.parse import quote

from ..context import NovaContext
from ..integrations.local_camera import local_camera_pool
from ..integrations.ntfy import send_ntfy
from ..notifications import Level, Notification
from ..runtime import Service, Topics
from ..runtime.errors import MissingDependency, MissingModel, SkillError
from .faces import FaceEngine, FaceStore


class SecurityService(Service):
    """Owns the watch loop, the enrolled-faces store and the alert pipeline."""

    name = "security"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.faces = FaceStore(ctx.paths.data_dir / "faces.json")
        self.engine = FaceEngine(ctx.paths.models_dir)
        self.armed = False
        self._watch_task: asyncio.Task[None] | None = None
        self._last_alert_at = 0.0
        self._consecutive_unknown = 0

    async def on_stop(self) -> None:
        await self.disarm()

    def describe(self) -> str:
        if self.armed:
            return f"watching ({self.ctx.settings.security.camera_name or 'no camera'})"
        return f"stood down, {len(self.faces.names())} face(s) enrolled"

    # -------------------------------------------------------------- control

    async def arm(self) -> str:
        settings = self.ctx.settings.security
        camera = self.resolve_camera()
        if camera is None:
            name = settings.camera_name or "(none set)"
            raise SkillError(
                f"'{name}' isn't a configured camera — set Security → Camera in settings first."
            )
        if not self.faces.names():
            raise SkillError(
                "no face is enrolled yet — say 'this is my face' while looking at the camera first."
            )
        if not self.engine.loaded:
            try:
                await asyncio.to_thread(self.engine.load)
            except (MissingDependency, MissingModel) as exc:
                raise SkillError(str(exc)) from exc
        if self.armed:
            return "Already watching."

        # Generated here rather than waiting for the first alert, so the
        # topic exists — and is visible in Settings — the moment room-watch
        # is armed for the first time, giving a chance to subscribe on ntfy
        # before anything actually happens, not only after.
        self._ensure_topic()

        self.armed = True
        self._consecutive_unknown = 0
        self._watch_task = self.spawn(self._watch_loop(camera.index), name="security-watch")
        self.log.info("security_armed", camera=camera.name)
        return "Watching now."

    async def disarm(self) -> str:
        if not self.armed:
            return "Already stood down."
        self.armed = False
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        self.log.info("security_disarmed")
        return "Stood down."

    def resolve_camera(self) -> Any:
        name = self.ctx.settings.security.camera_name
        for camera in self.ctx.settings.vision.named_cameras:
            if camera.name == name:
                return camera
        return None

    # ---------------------------------------------------------------- watch

    async def _watch_loop(self, index: int) -> None:
        try:
            while self.armed:
                await self._check_once(index)
                await asyncio.sleep(self.ctx.settings.security.poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("security_watch_failed")
            self.armed = False

    async def _check_once(self, index: int) -> None:
        try:
            frame = await local_camera_pool.read_bgr(index)
        except Exception as exc:  # noqa: BLE001 - a camera glitch must not stop the watch loop
            self.log.warning("security_frame_failed", error=str(exc))
            return

        observations = await asyncio.to_thread(self.engine.observe, frame)
        if not observations:
            self._consecutive_unknown = 0
            return

        settings = self.ctx.settings.security
        unknown_present = any(
            self.faces.match(obs.embedding, threshold=settings.match_threshold) is None
            for obs in observations
        )
        if not unknown_present:
            self._consecutive_unknown = 0
            return

        self._consecutive_unknown += 1
        if self._consecutive_unknown < settings.confirm_frames:
            return

        now = time.monotonic()
        if now - self._last_alert_at < settings.alert_cooldown_seconds:
            return
        self._last_alert_at = now
        self._consecutive_unknown = 0
        await self._raise_alert()

    # --------------------------------------------------------------- alert

    async def _raise_alert(self) -> None:
        settings = self.ctx.settings.security
        self.log.warning("security_alert_raised")

        # Spoken immediately and unconditionally — a security warning must not
        # wait behind NotificationService's busy/quiet-hours/defer handling,
        # the way an ordinary notification correctly does. Looked up by name
        # only (no isinstance check), the same as orchestrator.py's own
        # `_synthesise` does, and for the same reason: a test double for
        # either service is not a VoiceService/NotificationService subclass.
        voice = self.ctx.service("voice")
        if voice is not None:
            await voice.speak(settings.alert_message)

        # A visual record too, but not critical-level and not set to speak —
        # this alert has already been spoken above; a second, independent
        # speak trigger here would double it up.
        notifications = self.ctx.service("notifications")
        notification = Notification(
            title="Room-watch",
            body=settings.alert_message,
            level=Level.WARNING,
            icon="alert",
            source=self.name,
            timeout=0,
        )
        if notifications is not None:
            await notifications.raise_notification(notification)
        else:
            self.bus.publish(Topics.NOTIFICATION, notification.as_payload(), source=self.name)

        topic = self._ensure_topic()
        camera = self.resolve_camera()
        await send_ntfy(
            settings.ntfy_server,
            topic,
            title="N.O.V.A. room-watch",
            message=settings.alert_message,
            click_url=self._mobile_camera_url(camera) if camera is not None else "",
        )

    def _ensure_topic(self) -> str:
        settings = self.ctx.settings.security
        if settings.ntfy_topic:
            return settings.ntfy_topic
        # A topic on ntfy.sh is a public channel by name — long and random is
        # what keeps it effectively private, the same role a token plays
        # elsewhere in this app.
        topic = f"nova-room-{secrets.token_urlsafe(12)}"
        self.ctx.store.patch({"security": {"ntfy_topic": topic}})
        return topic

    def _mobile_camera_url(self, camera: Any) -> str:
        base = self.ctx.settings.transport.public_url.rstrip("/")
        if not base:
            return ""
        token = self.ctx.settings.transport.token
        slug = quote(f"local:{camera.name}", safe="")
        return f"{base}/?token={quote(token)}&camera={slug}"
