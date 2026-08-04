"""Smart home control through Home Assistant and MQTT."""

from __future__ import annotations

from typing import Annotated, Literal

from ...integrations.homeassistant import HAEntity
from ...integrations.services import HomeService
from ...memory.models import Entity as MemoryEntity
from ...runtime.errors import IntegrationError
from ..base import Param, Skill, tool

#: Domains offered to the model for a generic on/off, in resolution order.
_CONTROLLABLE = ("light", "switch", "fan", "media_player", "cover", "lock", "scene", "script")


class HomeSkill(Skill):
    name = "home"
    description = "Control lights, switches, climate, scenes and devices in the smart home."
    category = "Home"
    prompt_hint = (
        "For the current state of any light, switch, climate or other device — or to turn "
        "one on, off, or change it — call a home tool. Never answer from memory or a guess: "
        "a past conversation may be stale, but the tool always reflects what is true right now. "
        "If a name someone uses (often a brand, like 'the Govee lights') does not resolve, call "
        "list_devices to work out which real device they mean, then call remember_device_alias "
        "so that name resolves on its own from then on — do this silently, without announcing it."
    )

    def is_available(self) -> tuple[bool, str]:
        home = self.ctx.service("home", HomeService)
        if home is None:
            return False, "home integration is not connected"
        if home.ha is None and home.mqtt is None:
            return False, "neither Home Assistant nor MQTT is connected"
        return True, ""

    @property
    def home(self) -> HomeService:
        return self.ctx.require("home", HomeService)

    def context_lines(self) -> list[str]:
        home = self.ctx.service("home", HomeService)
        if home is None or home.ha is None:
            return []
        areas = sorted({e.area for e in home.ha.entities() if e.area})
        lines = [f"Home Assistant is connected with {home.ha.entity_count} entities."]
        if areas:
            lines.append(f"Areas in the home: {', '.join(areas[:15])}.")
        return lines

    # -------------------------------------------------------------- resolution

    async def _resolve(self, name: str, *, domain: str = "") -> HAEntity:
        """Resolve a spoken name to a live entity, falling back to a taught alias.

        A brand name like "the Govee lights" or "my Meross plug" often shares no
        words at all with the device's actual Home Assistant name, so the direct
        fuzzy match legitimately finding nothing is not really an error — it is
        a cue to check whether this name was taught before reaching for the
        "I can't find that" reply.
        """
        client = self.home.require_ha()
        if not client.resolve(name, domain=domain):
            aliased = await self._alias_lookup(name, domain=domain)
            if aliased is not None:
                return aliased
        return client.resolve_one(name, domain=domain)

    async def _alias_lookup(self, name: str, *, domain: str = "") -> HAEntity | None:
        memory = self.ctx.service("memory")
        if memory is None:
            return None
        client = self.home.require_ha()
        for candidate in await memory.find_entities(name, kind="ha_entity"):
            if domain and candidate.attributes.get("domain") != domain:
                continue
            try:
                return client.resolve_one(candidate.name, domain=domain)
            except IntegrationError:
                continue  # the taught name no longer matches a live entity
        return None

    # ----------------------------------------------------------------- lookup

    @tool("List smart home devices, optionally filtered by type or room.")
    async def list_devices(
        self,
        domain: Annotated[str, Param("Device type", examples=("light", "switch", "climate"))] = "",
        search: Annotated[str, Param("Filter by name or room")] = "",
    ) -> str:
        client = self.home.require_ha()
        entities = client.resolve(search, domain=domain) if search else client.entities(domain)
        if not entities:
            return "No matching devices."
        return "\n".join(f"{e.friendly_name} ({e.entity_id}): {e.state}" for e in entities[:40])

    @tool("Get the current state of a device or sensor.")
    async def device_state(
        self,
        name: Annotated[str, Param("Device name", examples=("kitchen light", "front door"))],
    ) -> str:
        entity = await self._resolve(name)
        return entity.describe()

    @tool("Get a summary of everything currently on in the home.")
    async def whats_on(self) -> str:
        client = self.home.require_ha()
        active = [
            e
            for e in client.entities()
            if e.domain in ("light", "switch", "fan", "media_player") and e.state == "on"
        ]
        if not active:
            return "Nothing is on."
        names = ", ".join(e.friendly_name for e in active[:20])
        return f"{len(active)} device{'s' if len(active) != 1 else ''} on: {names}."

    @tool(
        "Teach another name for a smart home device — a brand name, room nickname or anything "
        "the user prefers to call it instead of its Home Assistant name. Use this once you've "
        "worked out what device someone means by a name that did not resolve on its own, so it "
        "resolves immediately next time."
    )
    async def remember_device_alias(
        self,
        device: Annotated[str, Param("The device, findable by its current Home Assistant name")],
        alias: Annotated[str, Param("The alternate name to remember for it")],
    ) -> str:
        entity = await self._resolve(device)
        memory = self.ctx.service("memory")
        if memory is None:
            raise IntegrationError("home assistant", "memory is not available to remember that")
        await memory.upsert_entity(
            MemoryEntity(
                kind="ha_entity",
                name=entity.friendly_name,
                aliases=[alias],
                attributes={"domain": entity.domain, "area": entity.area},
            )
        )
        return f"Got it — I'll know {entity.friendly_name} as '{alias}' too."

    # ---------------------------------------------------------------- control

    @tool("Turn a device on or off.", mutating=True)
    async def set_device(
        self,
        name: Annotated[str, Param("Device or room name")],
        state: Annotated[Literal["on", "off"], Param("Desired state")],
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name)
        return await (client.turn_on if state == "on" else client.turn_off)(entity.entity_id)

    @tool("Set the brightness of a light as a percentage.", mutating=True)
    async def set_brightness(
        self,
        name: Annotated[str, Param("Light name")],
        percent: Annotated[int, Param("Brightness from 0 to 100")],
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name, domain="light")
        return await client.set_brightness(entity.entity_id, percent)

    @tool("Set the colour of a light by name.", mutating=True)
    async def set_light_colour(
        self,
        name: Annotated[str, Param("Light name")],
        colour: Annotated[str, Param("Colour name, e.g. 'warm white', 'red', 'blue'")],
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name, domain="light")
        await client.call_service("light", "turn_on", entity.entity_id, color_name=colour.lower())
        return f"set {entity.friendly_name} to {colour}"

    @tool("Set a thermostat target temperature.", mutating=True)
    async def set_temperature(
        self,
        name: Annotated[str, Param("Thermostat name")],
        temperature: Annotated[float, Param("Target temperature in the configured units")],
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name, domain="climate")
        return await client.set_temperature(entity.entity_id, temperature)

    @tool("Activate a scene.", mutating=True)
    async def activate_scene(
        self, name: Annotated[str, Param("Scene name", examples=("movie night", "bedtime"))]
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name, domain="scene")
        return await client.activate_scene(entity.entity_id)

    @tool("Run a Home Assistant automation or script.", mutating=True)
    async def run_automation(self, name: Annotated[str, Param("Automation or script name")]) -> str:
        client = self.home.require_ha()
        matches = client.resolve(name, domain="automation") or client.resolve(name, domain="script")
        if not matches:
            raise IntegrationError("home assistant", f"no automation or script called '{name}'")
        entity = matches[0]
        if entity.domain == "script":
            await client.call_service("script", "turn_on", entity.entity_id)
            return f"ran {entity.friendly_name}"
        return await client.trigger_automation(entity.entity_id)

    @tool("Lock or unlock a smart lock.", destructive=True)
    async def set_lock(
        self,
        name: Annotated[str, Param("Lock name")],
        action: Annotated[Literal["lock", "unlock"], Param("What to do")],
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name, domain="lock")
        await client.call_service("lock", action, entity.entity_id)
        return f"{action}ed {entity.friendly_name}"

    @tool("Open, close or stop a cover, blind or garage door.", mutating=True)
    async def set_cover(
        self,
        name: Annotated[str, Param("Cover name")],
        action: Annotated[Literal["open", "close", "stop"], Param("What to do")],
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name, domain="cover")
        await client.call_service("cover", f"{action}_cover", entity.entity_id)
        return f"{action}ing {entity.friendly_name}"

    @tool("Control media playback on a media player.", mutating=True)
    async def control_media(
        self,
        name: Annotated[str, Param("Media player name")],
        action: Annotated[
            Literal["play", "pause", "stop", "next", "previous", "volume_up", "volume_down"],
            Param("Playback action"),
        ],
    ) -> str:
        client = self.home.require_ha()
        entity = await self._resolve(name, domain="media_player")
        service = {
            "play": "media_play",
            "pause": "media_pause",
            "stop": "media_stop",
            "next": "media_next_track",
            "previous": "media_previous_track",
            "volume_up": "volume_up",
            "volume_down": "volume_down",
        }[action]
        await client.call_service("media_player", service, entity.entity_id)
        return f"{action} on {entity.friendly_name}"

    @tool(
        "Call a Home Assistant service directly, only for something no tool above covers. "
        "Prefer set_device, set_brightness etc. for ordinary control — they resolve the device "
        "name against what actually exists. A guessed entity id here that matches nothing is "
        "rejected rather than silently doing nothing.",
        destructive=True,
    )
    async def call_service(
        self,
        domain: Annotated[str, Param("Service domain, e.g. 'light'")],
        service: Annotated[str, Param("Service name, e.g. 'turn_on'")],
        entity_id: Annotated[str, Param("Target entity id; empty for none")] = "",
    ) -> str:
        client = self.home.require_ha()
        await client.call_service(domain, service, entity_id)
        return f"called {domain}.{service}"

    # ------------------------------------------------------------------- mqtt

    @tool("Read the most recent value published on an MQTT topic.")
    async def mqtt_value(self, topic: Annotated[str, Param("Topic or a suffix of one")]) -> str:
        client = self.home.require_mqtt()
        record = client.latest(topic)
        if record is None:
            known = ", ".join(client.topics()[:10]) or "none yet"
            return f"Nothing received on '{topic}'. Known topics: {known}."
        return f"{record.topic}: {record.decoded()}"

    @tool("Publish a message to an MQTT topic.", mutating=True)
    async def mqtt_publish(
        self,
        topic: Annotated[str, Param("Full topic to publish to")],
        payload: Annotated[str, Param("Message body")],
        retain: Annotated[bool, Param("Retain the message on the broker")] = False,
    ) -> str:
        client = self.home.require_mqtt()
        await client.publish(topic, payload, retain=retain)
        return f"published to {topic}"

    @tool("List MQTT topics that have reported a value.")
    async def mqtt_topics(self) -> str:
        client = self.home.require_mqtt()
        topics = client.topics()
        return ", ".join(topics[:40]) if topics else "No topics have reported yet."
