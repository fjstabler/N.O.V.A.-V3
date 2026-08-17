"""The person alarm: arm/disarm the auto watch by voice.

The watching itself lives in :class:`nova.personwatch.PersonWatchService`; this
is the voice-facing control surface over it, the same split as security's
room-watch. Unlike that one, this needs no enrolled faces — it alerts on anyone.
"""

from __future__ import annotations

from ...personwatch import PersonWatchService
from ..base import Skill, tool


class PersonWatchSkill(Skill):
    name = "personwatch"
    description = "Watch the camera and alert when anyone comes in — no face setup."
    category = "Security"
    prompt_hint = (
        "To WATCH the room and be alerted when SOMEONE COMES IN — 'watch my room', 'keep an eye "
        "on my room', 'tell me if anyone comes in', 'let me know if someone enters', 'guard my "
        "bedroom' — call arm_person_alarm. It detects any person on the webcam and needs no face "
        "setup, so it is the right choice for a plain 'watch my room'. Say 'stand down' or 'stop "
        "watching' to turn it off. (Security's face-based room-watch is only for the narrower case "
        "of recognising specific enrolled faces / alerting on strangers.)"
    )

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.vision.camera_enabled:
            return False, "the camera is disabled in settings"
        if self.ctx.service("personwatch") is None:
            return False, "the person-watch service is not running"
        return True, ""

    @property
    def service(self) -> PersonWatchService:
        return self.ctx.require("personwatch", PersonWatchService)

    def context_lines(self) -> list[str]:
        service = self.ctx.service("personwatch", PersonWatchService)
        if service is None:
            return []
        if service.armed:
            return ["The person alarm is armed — watching your room for anyone coming in."]
        return ["The person alarm is off (stood down)."]

    @tool(
        "Arm the person alarm: watch the webcam and alert you when anyone comes into the room. "
        "No face setup needed — this is what 'watch my room', 'tell me if someone comes in' and "
        "'let me know if anyone enters' mean. It speaks the alert if you're home and pushes it to "
        "your phone if you're out."
    )
    async def arm_person_alarm(self) -> str:
        return await self.service.arm()

    @tool("Turn the person alarm off — stop watching the room. This is 'stand down'.")
    async def disarm_person_alarm(self) -> str:
        return await self.service.disarm()
