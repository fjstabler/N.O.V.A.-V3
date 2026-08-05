"""Bringing things up on screen: maps and live camera views.

Where every other skill answers in words, this one answers by putting
something on the desktop app's display — the map or the camera feed itself,
not a description of it (that is what vision.py's `look_at_camera` is for; a
camera named here is *shown*, not looked at and narrated).

`show_camera` resolves against two sources under one name: a local device
attached to this machine (`vision.named_cameras`) and a Home Assistant
`camera.*` entity (Ring and anything else HA exposes). From the model's side,
and the person asking, there is only one kind of "show me the front door" or
"show me the bedroom" — which source actually serves it is an implementation
detail this skill hides.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from ...integrations.homeassistant import HAEntity
from ...integrations.services import HomeService
from ...runtime import Topics
from ...runtime.errors import IntegrationError, SkillError
from ..base import Param, Skill, tool


class DisplaySkill(Skill):
    name = "display"
    description = "Show a map or a live camera view on the screen."
    category = "Display"
    prompt_hint = (
        "When someone asks to see something — a place on a map, or a camera view like "
        "'show me the front door' or 'show me the bedroom' — call show_map or show_camera "
        "rather than describing it in words; that is what actually puts it on screen. Keep "
        "the spoken reply to a short line like 'Here it is' — the picture is the answer, not "
        "the sentence. If a camera name does not resolve, call home_list_devices to find its "
        "real Home Assistant name, then home_remember_device_alias to teach the name that was "
        "actually used — do this silently, without announcing it, the same as for any other "
        "device that goes by a brand name rather than its Home Assistant one."
    )

    @tool("Show a map of a place on screen.")
    async def show_map(
        self,
        location: Annotated[
            str,
            Param("A place name, address, or landmark", examples=("London", "the Eiffel Tower")),
        ],
    ) -> str:
        lat, lon, label = await self._geocode(location)
        self.ctx.bus.publish(
            Topics.UI_SURFACE_SHOW,
            {"kind": "map", "title": label, "lat": lat, "lon": lon},
            source=self.name,
        )
        return f"Here's {label} on the map."

    @tool("Show a live view from a named camera — a room, a doorbell, anything configured.")
    async def show_camera(
        self,
        name: Annotated[str, Param("Which camera", examples=("front door", "bedroom"))],
    ) -> str:
        local = self._find_local_camera(name)
        if local is not None:
            slug = f"local:{local.name}"
            title = local.name
        else:
            entity = await self._resolve_ha_camera(name)
            slug = f"ha:{entity.entity_id}"
            title = entity.friendly_name
        self.ctx.bus.publish(
            Topics.UI_SURFACE_SHOW,
            {"kind": "camera", "title": title, "streamPath": f"/camera/{quote(slug, safe='')}"},
            source=self.name,
        )
        return f"Here's the {title} camera."

    # ------------------------------------------------------------ resolution

    def _find_local_camera(self, name: str):
        needle = name.strip().lower()
        for camera in self.ctx.settings.vision.named_cameras:
            if camera.name.strip().lower() == needle:
                return camera
        return None

    async def _resolve_ha_camera(self, name: str) -> HAEntity:
        home = self.ctx.service("home", HomeService)
        if home is None or home.ha is None:
            raise SkillError(
                f"'{name}' isn't a configured camera, and Home Assistant isn't connected to "
                "check for one there either."
            )
        # A brand name ("my Ring camera") often shares no words with the entity's
        # actual friendly name, exactly the gap `home_remember_device_alias`
        # exists to close — check what has been taught before giving up.
        if not home.ha.resolve(name, domain="camera"):
            aliased = await self._alias_lookup_camera(name)
            if aliased is not None:
                return aliased
        return home.ha.resolve_one(name, domain="camera")

    async def _alias_lookup_camera(self, name: str) -> HAEntity | None:
        memory = self.ctx.service("memory")
        home = self.ctx.service("home", HomeService)
        if memory is None or home is None or home.ha is None:
            return None
        for candidate in await memory.find_entities(name, kind="ha_entity"):
            if candidate.attributes.get("domain") != "camera":
                continue
            try:
                return home.ha.resolve_one(candidate.name, domain="camera")
            except IntegrationError:
                continue  # the taught name no longer matches a live entity
        return None

    # ----------------------------------------------------------------- geocode

    async def _geocode(self, location: str) -> tuple[float, float, str]:
        """Free, keyless geocoding via OSM Nominatim — a name to a point on the map."""
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": location, "format": "json", "limit": 1},
                # Nominatim's usage policy requires an identifying User-Agent.
                headers={"User-Agent": "nova-assistant/3.0 (local personal use)"},
            )
        response.raise_for_status()
        results = response.json()
        if not results:
            raise SkillError(f"I couldn't find '{location}' on the map.")
        best = results[0]
        label = str(best.get("display_name") or location).split(",")[0]
        return float(best["lat"]), float(best["lon"]), label
