"""Frigate cameras: ask what the on-device detector can actually see.

The reliable answer to "is anyone at the door" / "is anyone in the office" —
read straight from Frigate's person/object detection through Home Assistant,
rather than a webcam trying to catch a face head-on. Alerts are handled
separately by FrigateService; this is the on-demand query surface.
"""

from __future__ import annotations

from typing import Annotated

from ...integrations.frigate import active_cameras, camera_name, detection_sensors
from ...integrations.services import HomeService
from ..base import Param, Skill, tool


class FrigateSkill(Skill):
    name = "frigate"
    description = "See what Frigate's cameras detect right now — reliable person/object detection."
    category = "Vision"
    prompt_hint = (
        "For 'is anyone at the door', 'is anyone in the office', 'is someone in the driveway', "
        "'who's around' — call check_cameras. It reads Frigate's own on-device detection through "
        "Home Assistant, which is reliable, rather than guessing from a webcam. Prefer it over "
        "vision's look_at_camera for whether a person is present."
    )

    def is_available(self) -> tuple[bool, str]:
        settings = self.ctx.settings.frigate
        if not settings.enabled:
            return False, "Frigate integration disabled in settings"
        home = self.ctx.service("home", HomeService)
        if home is None or home.ha is None:
            return False, "Home Assistant is not connected"
        if not detection_sensors(home.ha.entities(), settings.object_type):
            return False, f"no Frigate {settings.object_type} sensors found in Home Assistant"
        return True, ""

    @property
    def home(self) -> HomeService:
        return self.ctx.require("home", HomeService)

    def context_lines(self) -> list[str]:
        home = self.ctx.service("home", HomeService)
        if home is None or home.ha is None:
            return []
        obj = self.ctx.settings.frigate.object_type
        cameras = {
            camera_name(s.entity_id, obj) for s in detection_sensors(home.ha.entities(), obj)
        }
        if not cameras:
            return []
        return [f"Frigate cameras with {obj} detection: {', '.join(sorted(cameras))}."]

    @tool(
        "Check what Frigate's cameras can see right now — reliable on-device detection through "
        "Home Assistant. Use for 'is anyone at the door', 'anyone in the office', 'who is around'."
    )
    async def check_cameras(self) -> str:
        client = self.home.require_ha()
        obj = self.ctx.settings.frigate.object_type
        active = active_cameras(client.entities(), obj)
        if not active:
            return f"No {obj} on any Frigate camera right now."
        parts = []
        for camera, count in active:
            where = f"the {camera} camera" if camera else "a camera"
            parts.append(f"{count} on {where}" if count else where)
        return f"Frigate sees a {obj} on {', '.join(parts)}."

    @tool(
        "Check a specific Frigate camera for the detected object — 'is anyone at the front door'."
    )
    async def check_camera(
        self,
        camera: Annotated[str, Param("Frigate camera name", examples=("front door", "driveway"))],
    ) -> str:
        client = self.home.require_ha()
        obj = self.ctx.settings.frigate.object_type
        needle = camera.strip().lower()
        matches = [
            (cam, count)
            for cam, count in active_cameras(client.entities(), obj)
            if needle in cam or cam in needle
        ]
        if matches:
            _, count = matches[0]
            amount = f"{count} " if count else "a "
            return f"Yes — {amount}{obj} on the {camera} camera right now."
        # Distinguish "camera exists, nothing there" from "no such camera".
        known = {camera_name(s.entity_id, obj) for s in detection_sensors(client.entities(), obj)}
        if any(needle in cam or cam in needle for cam in known):
            return f"No {obj} on the {camera} camera right now."
        listed = ", ".join(sorted(known)) or "none found"
        return f"I don't have a Frigate camera called '{camera}'. Cameras: {listed}."
