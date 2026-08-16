"""Smart home control through Home Assistant and MQTT."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import ValidationError

from ...config.schema import Routine, RoutineStep
from ...integrations.homeassistant import HAEntity, HomeAssistantClient
from ...integrations.services import HomeService
from ...memory.models import Entity as MemoryEntity
from ...runtime.errors import IntegrationError
from ..base import Param, Skill, tool

#: Domains offered to the model for a generic on/off, in resolution order.
_CONTROLLABLE = ("light", "switch", "fan", "media_player", "cover", "lock", "scene", "script")

#: Domains a blanket "turn the room off/on" should touch. Covers and locks are
#: excluded on purpose — closing a blind or unlocking a door is never what
#: "turn everything off" means, and both need their own explicit intent.
_ROOM_CONTROLLABLE = ("light", "switch", "fan", "media_player")


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
        routines = self.ctx.settings.routines.items
        if routines:
            names = ", ".join(f"'{r.name}'" for r in routines[:12])
            lines.append(
                f"Saved routines — run one with run_routine when the user says its name: {names}."
            )
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

    @tool(
        "Turn a whole room's devices on or off at once — 'turn off the bedroom', "
        "'turn off all the lights in the kitchen'. Pass a device type in `only` to "
        "limit it to that kind (e.g. only lights); leave it blank for lights, "
        "switches, fans and media players together. Covers and locks are never "
        "included — use set_cover or set_lock for those.",
        mutating=True,
    )
    async def control_room(
        self,
        room: Annotated[str, Param("Room / area name", examples=("bedroom", "living room"))],
        action: Annotated[Literal["on", "off"], Param("Turn the room on or off")],
        only: Annotated[
            str, Param("Limit to one device type", examples=("light", "switch", "fan"))
        ] = "",
    ) -> str:
        client = self.home.require_ha()
        domains = (only,) if only else _ROOM_CONTROLLABLE
        entities = client.in_area(room, domains=domains)
        if not entities:
            known = ", ".join(client.areas()) or "none are named"
            kind = f" {only}" if only else ""
            raise IntegrationError(
                "home assistant",
                f"I couldn't find any{kind} devices in '{room}'. Rooms I know: {known}.",
            )
        area = entities[0].area
        count = await client.set_many([e.entity_id for e in entities], on=action == "on")
        noun = "device" if count == 1 else "devices"
        return f"Turned {action} {count} {noun} in {area}."

    @tool(
        "Get an overview of the whole home: what's on, thermostat readings, and "
        "anything left open or unlocked. Use for 'how's the house', 'is everything "
        "off', 'did I leave anything on'."
    )
    async def home_overview(self) -> str:
        client = self.home.require_ha()
        entities = client.entities()
        on_devices = [
            e
            for e in entities
            if e.domain in ("light", "switch", "fan", "media_player") and e.state == "on"
        ]
        open_covers = [e for e in entities if e.domain == "cover" and e.state == "open"]
        unlocked = [e for e in entities if e.domain == "lock" and e.state == "unlocked"]

        lines: list[str] = []
        if on_devices:
            names = ", ".join(e.friendly_name for e in on_devices[:12])
            more = f" (+{len(on_devices) - 12} more)" if len(on_devices) > 12 else ""
            lines.append(f"{len(on_devices)} on: {names}{more}.")
        else:
            lines.append("Nothing is on.")
        climate = [
            f"{e.friendly_name} {e.attributes['current_temperature']}°"
            for e in entities
            if e.domain == "climate" and e.attributes.get("current_temperature") is not None
        ]
        if climate:
            lines.append("Climate: " + ", ".join(climate) + ".")
        if open_covers:
            lines.append("Open: " + ", ".join(e.friendly_name for e in open_covers) + ".")
        if unlocked:
            lines.append("Unlocked: " + ", ".join(e.friendly_name for e in unlocked) + ".")
        return "\n".join(lines)

    @tool(
        "Get temperatures across the home — thermostats and temperature sensors. "
        "Use for 'how warm is it', 'what's the temperature upstairs'."
    )
    async def climate_summary(self) -> str:
        client = self.home.require_ha()
        lines: list[str] = []
        for entity in client.entities("climate"):
            bits = [entity.friendly_name]
            if (current := entity.attributes.get("current_temperature")) is not None:
                bits.append(f"now {current}°")
            if (target := entity.attributes.get("temperature")) is not None:
                bits.append(f"set to {target}°")
            if entity.state:
                bits.append(f"({entity.state})")
            lines.append(" ".join(bits))
        for entity in client.entities("sensor"):
            if entity.attributes.get("device_class") == "temperature":
                unit = entity.attributes.get("unit_of_measurement", "°")
                lines.append(f"{entity.friendly_name}: {entity.state}{unit}")
        if not lines:
            return "No thermostats or temperature sensors found."
        return "\n".join(lines)

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

    # --------------------------------------------------------------- routines

    @tool(
        "Create or replace a routine — a phrase that runs several home actions at once, like an "
        'Alexa routine. `steps` is a JSON array of {"action","target","value"} objects. "action" '
        "is one of: on, off (a single device OR a whole room), brightness (value = percent), "
        'colour (value = a colour name), temperature (value = degrees), scene, script. "target" '
        'is the device, room or scene name. Example — a "movie time" routine: '
        '[{"action":"off","target":"living room"},{"action":"on","target":"tv"},'
        '{"action":"brightness","target":"hallway","value":"20"}]. Save it under the exact phrase '
        "the user will say to trigger it.",
        mutating=True,
    )
    async def create_routine(
        self,
        name: Annotated[str, Param("The trigger phrase", examples=("movie time", "goodnight"))],
        steps: Annotated[str, Param("JSON array of {action, target, value} steps")],
    ) -> str:
        clean = name.strip()
        if not clean:
            raise IntegrationError("routine", "a routine needs a name to trigger it")
        try:
            raw = json.loads(steps)
        except json.JSONDecodeError as exc:
            raise IntegrationError("routine", f"the steps weren't valid JSON: {exc}") from exc
        if not isinstance(raw, list) or not raw:
            raise IntegrationError("routine", "a routine needs at least one step")
        try:
            parsed = [RoutineStep.model_validate(step) for step in raw]
        except ValidationError as exc:
            raise IntegrationError(
                "routine",
                "each step needs an action (on, off, brightness, colour, temperature, scene or "
                f"script) and a target — {exc.error_count()} step(s) didn't fit",
            ) from exc

        others = [r for r in self.ctx.settings.routines.items if r.name.lower() != clean.lower()]
        replaced = len(others) != len(self.ctx.settings.routines.items)
        routine = Routine(name=clean, steps=parsed)
        self.ctx.store.patch({"routines": {"items": [r.model_dump() for r in [*others, routine]]}})
        verb = "Updated" if replaced else "Saved"
        return f"{verb} '{clean}' with {len(parsed)} step(s). Say '{clean}' and I'll run it."

    @tool(
        "Run a saved routine by name. Use this whenever the user says a routine's trigger phrase "
        "(the saved routines are listed in the environment), or asks to run one."
    )
    async def run_routine(self, name: Annotated[str, Param("The routine to run")]) -> str:
        routine = self._find_routine(name)
        if routine is None:
            known = ", ".join(r.name for r in self.ctx.settings.routines.items) or "none saved yet"
            raise IntegrationError(
                "routine", f"I don't have a routine called '{name}'. Saved routines: {known}."
            )
        client = self.home.require_ha()
        done = 0
        failures: list[str] = []
        for step in routine.steps:
            try:
                await self._run_step(client, step)
                done += 1
            except IntegrationError as exc:
                failures.append(f"{step.action} {step.target} ({exc.message})")
        if not failures:
            return f"Done — ran '{routine.name}' ({done} action{'s' if done != 1 else ''})."
        ran = f"ran {done} of {len(routine.steps)}" if done else "couldn't run any of it"
        return f"Partly ran '{routine.name}' — {ran}. Trouble with: {'; '.join(failures)}."

    @tool("List the saved routines and what each one does.")
    async def list_routines(self) -> str:
        items = self.ctx.settings.routines.items
        if not items:
            return "No routines saved yet — tell me what a phrase should do and I'll save it."
        return "\n".join(
            f"{r.name}: " + ", ".join(_describe_step(s) for s in r.steps) for r in items
        )

    @tool("Delete a saved routine.", mutating=True)
    async def delete_routine(self, name: Annotated[str, Param("The routine to delete")]) -> str:
        routine = self._find_routine(name)
        if routine is None:
            raise IntegrationError("routine", f"I don't have a routine called '{name}'")
        remaining = [
            r for r in self.ctx.settings.routines.items if r.name.lower() != routine.name.lower()
        ]
        self.ctx.store.patch({"routines": {"items": [r.model_dump() for r in remaining]}})
        return f"Deleted the routine '{routine.name}'."

    def _find_routine(self, name: str) -> Routine | None:
        needle = name.strip().lower()
        items = self.ctx.settings.routines.items
        for routine in items:
            if routine.name.lower() == needle:
                return routine
        for routine in items:
            if needle and (needle in routine.name.lower() or routine.name.lower() in needle):
                return routine
        return None

    async def _run_step(self, client: HomeAssistantClient, step: RoutineStep) -> None:
        action, target, value = step.action, step.target, step.value
        if action in ("on", "off"):
            on = action == "on"
            # An exact room name is a room command; a real light happens to sit in
            # an area too, so only the exact match short-circuits to the whole room.
            if target.strip().lower() in {a.lower() for a in client.areas()}:
                room = client.in_area(target, domains=_ROOM_CONTROLLABLE)
                if room:
                    await client.set_many([e.entity_id for e in room], on=on)
                    return
            try:
                entity = await self._resolve(target)
            except IntegrationError:
                entity = None
            if entity is not None:
                await (client.turn_on if on else client.turn_off)(entity.entity_id)
                return
            room = client.in_area(target, domains=_ROOM_CONTROLLABLE)
            if room:
                await client.set_many([e.entity_id for e in room], on=on)
                return
            raise IntegrationError("home assistant", f"no device or room called '{target}'")
        if action == "brightness":
            entity = await self._resolve(target, domain="light")
            await client.set_brightness(entity.entity_id, _to_int(value, 100))
            return
        if action == "colour":
            entity = await self._resolve(target, domain="light")
            await client.call_service(
                "light", "turn_on", entity.entity_id, color_name=(value or "white").lower()
            )
            return
        if action == "temperature":
            entity = await self._resolve(target, domain="climate")
            await client.set_temperature(entity.entity_id, _to_float(value, 20.0))
            return
        if action == "scene":
            entity = await self._resolve(target, domain="scene")
            await client.activate_scene(entity.entity_id)
            return
        if action == "script":
            entity = await self._resolve(target, domain="script")
            await client.call_service("script", "turn_on", entity.entity_id)

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


def _describe_step(step: RoutineStep) -> str:
    if step.action in ("on", "off"):
        return f"turn {step.action} {step.target}"
    if step.action == "brightness":
        return f"{step.target} to {step.value or '100'}%"
    if step.action == "colour":
        return f"{step.target} {step.value or 'white'}"
    if step.action == "temperature":
        return f"{step.target} to {step.value or '20'}°"
    return f"{step.action} {step.target}"


def _to_int(value: str, default: int) -> int:
    digits = "".join(c for c in value if c.isdigit() or c == "-")
    try:
        return int(digits)
    except ValueError:
        return default


def _to_float(value: str, default: float) -> float:
    allowed = "".join(c for c in value if c.isdigit() or c in ".-")
    try:
        return float(allowed)
    except ValueError:
        return default
