"""Room-watch: arm/disarm and face enrollment by voice.

The actual watching lives in `security.SecurityService` — this skill is the
voice-facing control surface over it, the same split as `voice_activate`
sits over `VoiceService`.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from ...integrations.local_camera import local_camera_pool
from ...runtime.errors import MissingDependency, MissingModel, SkillError
from ...security.service import SecurityService
from ..base import Param, Skill, tool


class SecuritySkill(Skill):
    name = "security"
    description = "Arm or disarm room-watch, and teach it whose face to leave alone."
    category = "Security"
    prompt_hint = (
        "This is the FACE-recognition room-watch: it alerts on faces it does NOT recognise, and "
        "needs faces enrolled first. Use arm_room_watch only when the user specifically wants "
        "recognition — 'watch for strangers', 'alert me if someone I don't know comes in', 'tell "
        "me if it isn't me'. For a plain 'watch my room' / 'keep an eye on my room' / 'tell me if "
        "someone comes in' with no mention of recognising faces, prefer personwatch's "
        "arm_person_alarm instead — it needs no setup. Neither is display's show_camera (a one-off "
        "look, not ongoing protection). Use learn_face when someone says 'this is my face' or "
        "'remember what I look like' while looking at the camera — that is enrollment, not a "
        "request to see anything."
    )

    @property
    def service(self) -> SecurityService:
        return self.ctx.require("security", SecurityService)

    def is_available(self) -> tuple[bool, str]:
        if self.ctx.service("security", SecurityService) is None:
            return False, "security service is not running"
        return True, ""

    def context_lines(self) -> list[str]:
        service = self.ctx.service("security", SecurityService)
        if service is None:
            return []
        if service.armed:
            camera = self.ctx.settings.security.camera_name
            return [f"Room-watch is currently armed, watching '{camera}'."]
        enrolled = len(service.faces.names())
        return [f"Room-watch is stood down. {enrolled} face(s) enrolled."]

    @tool(
        "Arm room-watch: start alerting if anyone but an enrolled face is seen on the camera. "
        "This is what 'watch my room', 'watch the bedroom', 'guard my room' and similar mean — "
        "ongoing protection, not a one-off look (that is display's show_camera instead)."
    )
    async def arm_room_watch(self) -> str:
        return await self.service.arm()

    @tool("Disarm room-watch — stop alerting on unrecognised faces.")
    async def disarm_room_watch(self) -> str:
        return await self.service.disarm()

    @tool(
        "Enroll a face so room-watch recognises it and does not alert on it — "
        "call while the person is looking at the camera. This is enrollment "
        "('this is my face'), not a request to view or arm anything."
    )
    async def learn_face(
        self,
        name: Annotated[str, Param("Whose face this is")] = "Fin",
    ) -> str:
        service = self.service
        camera = service.resolve_camera()
        if camera is None:
            configured = self.ctx.settings.security.camera_name or "(none set)"
            raise SkillError(
                f"'{configured}' isn't a configured camera — "
                "set Security → Camera in settings first."
            )
        if not service.engine.loaded:
            try:
                await asyncio.to_thread(service.engine.load)
            except (MissingDependency, MissingModel) as exc:
                raise SkillError(str(exc)) from exc

        frame = await local_camera_pool.read_bgr(camera.index)
        observations = await asyncio.to_thread(service.engine.observe, frame)
        if not observations:
            raise SkillError("I couldn't see a face clearly — try looking directly at the camera.")
        # The largest box is the closest/most prominent face — the person
        # actively enrolling, if more than one face happens to be in frame.
        closest = max(observations, key=lambda obs: obs.bbox[2] * obs.bbox[3])
        service.faces.add(name, closest.embedding)
        return f"Got it — I'll know {name} on sight."
