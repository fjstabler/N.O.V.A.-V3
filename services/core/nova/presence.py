"""PresenceService: is the user in the room, and how should we reach them?

Two questions, one service. First, *presence*: N.O.V.A. decides whether the
person it belongs to is here right now. It uses two independent signals so it
stays useful on any hardware:

* **recent interaction** — if you spoke to N.O.V.A. a moment ago you are plainly
  in earshot; this needs no camera at all;
* **a camera glance** — a single frame is captured and checked for an *enrolled*
  face (the same faces room-watch already knows). Only when the interaction
  signal is cold, and only momentarily, so the camera light is not left on.

Second, *reaching you*: a notification is routed by that presence. In the room,
N.O.V.A. simply says it out loud. Away, it pushes to your phone over ntfy — and
either way it also leaves a panel on screen, so nothing is ever lost to a bad
guess. The three channels are independent: a missing voice service, an unset
push topic or disabled notifications each degrade on their own.

This is the complement of :mod:`nova.security.service`, which watches for faces
it does *not* know; here the whole point is recognising the one it does.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass

from .context import NovaContext
from .integrations.local_camera import local_camera_pool
from .integrations.ntfy import send_ntfy
from .notifications import Level, Notification
from .runtime import Service, Topics
from .runtime.errors import DegradedCapability


@dataclass(slots=True)
class Presence:
    """A presence reading and how it was reached."""

    present: bool
    #: One of "interaction", "watch", "camera", "unknown".
    method: str
    detail: str = ""


class PresenceService(Service):
    """Decides whether the user is present, and reaches them accordingly."""

    name = "presence"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        # -inf, not 0.0: time.monotonic()'s zero is an arbitrary reference (often
        # boot), so `now - 0.0` on a freshly-booted machine can look "recent" and
        # wrongly report presence. -inf makes "no interaction yet" always old.
        self._last_interaction = float("-inf")
        self._cache: Presence | None = None
        self._cache_at = float("-inf")

    async def on_start(self) -> None:
        # Any of these means a human is interacting with N.O.V.A. right now.
        for topic in (Topics.TURN_STARTED, Topics.TRANSCRIPT_FINAL, Topics.WAKE_DETECTED):
            self.bus.subscribe(topic, self._note_interaction)

    def describe(self) -> str:
        seen = time.monotonic() - self._last_interaction
        if seen < self.ctx.settings.presence.interaction_window_seconds:
            return "user present (recent interaction)"
        return "presence unknown"

    def _note_interaction(self, _event: object) -> None:
        self._last_interaction = time.monotonic()
        self._cache = None  # a fresh signal beats any cached reading

    # ------------------------------------------------------------- detection

    async def is_present(self, *, force: bool = False) -> Presence:
        """Best current reading, cached briefly so repeat calls don't re-probe."""
        settings = self.ctx.settings.presence
        now = time.monotonic()
        if not force and self._cache is not None and now - self._cache_at < settings.cache_seconds:
            return self._cache
        result = await self._determine()
        self._cache = result
        self._cache_at = now
        return result

    async def _determine(self) -> Presence:
        settings = self.ctx.settings.presence
        now = time.monotonic()

        # 1. You spoke to me recently — the strongest signal, and camera-free.
        if now - self._last_interaction <= settings.interaction_window_seconds:
            return Presence(True, "interaction", "you spoke to me a moment ago")

        # 2. Room-watch, if armed, is already looking and may have just seen you.
        security = self.ctx.service("security")
        last_known = getattr(security, "last_known_face_at", None)
        if last_known is not None and now - last_known <= settings.interaction_window_seconds:
            return Presence(True, "watch", "room-watch can see you")

        # 3. A momentary camera glance for an enrolled face.
        if settings.use_camera:
            seen = await self._camera_shows_known_face()
            if seen is True:
                return Presence(True, "camera", "I can see you on the camera")
            if seen is False:
                return Presence(False, "camera", "no familiar face on the camera")

        # 4. Nothing to go on.
        return Presence(False, "unknown", "I can't tell where you are")

    async def _camera_shows_known_face(self) -> bool | None:
        """True/False if the camera could decide; None if it could not be used."""
        index = self._presence_camera_index()
        if index is None:
            return None
        security = self.ctx.service("security")
        if security is None:
            return None
        engine, faces = security.engine, security.faces
        if not faces.names():
            return None  # nobody enrolled — a face means nothing to us
        if not engine.loaded:
            try:
                await asyncio.to_thread(engine.load)
            except Exception:  # noqa: BLE001 - no model/opencv → presence falls back
                return None
        try:
            frame = await local_camera_pool.read_bgr(index)
            observations = await asyncio.to_thread(engine.observe, frame)
        except Exception as exc:  # noqa: BLE001 - a camera glitch is not "absent"
            self.log.warning("presence_camera_failed", error=str(exc))
            return None
        threshold = self.ctx.settings.presence.match_threshold
        for obs in observations:
            if faces.match(obs.embedding, threshold=threshold) is not None:
                return True
        return False

    def _presence_camera_index(self) -> int | None:
        settings = self.ctx.settings.presence
        name = settings.camera_name or self.ctx.settings.security.camera_name
        if not name:
            return None
        for camera in self.ctx.settings.vision.named_cameras:
            if camera.name == name:
                return camera.index
        return None

    # --------------------------------------------------------------- routing

    async def reach_user(
        self,
        title: str,
        body: str = "",
        *,
        level: str = "info",
        click_url: str = "",
    ) -> str:
        """Deliver a notification by whichever channel actually reaches the user."""
        presence = await self.is_present()
        if presence.present:
            return await self._deliver_spoken(title, body, level, presence)
        return await self._deliver_pushed(title, body, level, click_url)

    async def _deliver_spoken(self, title: str, body: str, level: str, presence: Presence) -> str:
        spoken = False
        voice = self.ctx.service("voice")
        if voice is not None:
            spoken_text = f"{title}. {body}".strip() if body else title
            await voice.speak(spoken_text)
            spoken = True
        # A panel too, but silent — it has already been spoken aloud above.
        await self._panel(title, body, level, speak=not spoken)
        if spoken:
            return f"Told you out loud — {presence.detail}."
        return "Shown on screen (no voice output available)."

    async def _deliver_pushed(self, title: str, body: str, level: str, click_url: str) -> str:
        pushed = await self._push(title, body, click_url)
        # Leave it on screen as well, so it's waiting when you get back.
        await self._panel(title, body, level, speak=False)
        if pushed:
            return "You're not in the room, so I sent it to your phone."
        if not self.ctx.settings.notifications.push_enabled:
            return "You're away and phone push is off, so I've left it on screen for you."
        return "You're away and I couldn't reach your phone, so I've left it on screen for you."

    async def _panel(self, title: str, body: str, level: str, *, speak: bool) -> None:
        notifications = self.ctx.service("notifications")
        note = Notification(
            title=title,
            body=body,
            level=_coerce_level(level),
            source=self.name,
            speak=speak,
        )
        if notifications is not None:
            await notifications.raise_notification(note)
        else:
            self.bus.publish(Topics.NOTIFICATION, note.as_payload(), source=self.name)

    async def _push(self, title: str, body: str, click_url: str) -> bool:
        settings = self.ctx.settings.notifications
        if not settings.push_enabled:
            return False
        topic = self._ensure_push_topic()
        if not topic:
            return False
        return await send_ntfy(
            settings.push_server,
            topic,
            title=title,
            message=body or title,
            click_url=click_url,
        )

    def _ensure_push_topic(self) -> str:
        """Return the phone-push topic, generating a private one on first use."""
        settings = self.ctx.settings.notifications
        if settings.push_topic:
            return settings.push_topic
        # Like security's topic: on ntfy.sh a topic name *is* the credential, so a
        # long random one is what keeps it private. Persisted so it stays stable.
        topic = f"nova-{secrets.token_urlsafe(12)}"
        try:
            self.ctx.store.patch({"notifications": {"push_topic": topic}})
        except DegradedCapability:
            return ""
        return topic


def _coerce_level(value: str) -> Level:
    try:
        return Level(value)
    except ValueError:
        return Level.INFO
