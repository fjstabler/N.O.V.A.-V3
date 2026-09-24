"""Home Assistant integration.

REST for commands, WebSocket for live state. The WebSocket connection matters:
polling every light every few seconds is wasteful and always slightly stale,
whereas a subscription gives N.O.V.A. an entity cache that is correct the moment
someone flips a physical switch — so "is the kitchen light on" is answered from
memory, instantly, and correctly.

Entity resolution is fuzzy on purpose. Speech gives us "the kitchen lamp", not
``light.kitchen_lamp_2``, so names, friendly names, aliases and areas are all
searched before giving up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..runtime.errors import IntegrationError
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: Domains that respond to a plain on/off.
TOGGLEABLE = frozenset({"light", "switch", "fan", "input_boolean", "automation", "script", "siren"})


@dataclass(slots=True)
class HAEntity:
    entity_id: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)
    last_changed: str = ""

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def friendly_name(self) -> str:
        return self.attributes.get("friendly_name") or self.entity_id.split(".", 1)[-1].replace(
            "_", " "
        )

    @property
    def area(self) -> str:
        return self.attributes.get("area") or ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "entityId": self.entity_id,
            "name": self.friendly_name,
            "domain": self.domain,
            "state": self.state,
            "area": self.area,
            "attributes": {
                k: v
                for k, v in self.attributes.items()
                if k
                in (
                    "brightness",
                    "color_temp",
                    "temperature",
                    "current_temperature",
                    "hvac_action",
                    "media_title",
                    "volume_level",
                    "battery_level",
                    "device_class",
                    "unit_of_measurement",
                    "rgb_color",
                )
            },
        }

    def describe(self) -> str:
        text = f"{self.friendly_name} is {self.state}"
        unit = self.attributes.get("unit_of_measurement")
        if unit and self.state.replace(".", "").replace("-", "").isdigit():
            text = f"{self.friendly_name} is {self.state}{unit}"
        if brightness := self.attributes.get("brightness"):
            text += f" at {round(int(brightness) / 255 * 100)}% brightness"
        if temperature := self.attributes.get("current_temperature"):
            text += f", currently {temperature}°"
        return text


class HomeAssistantClient:
    """REST commands plus a live WebSocket entity cache."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        exposed_domains: tuple[str, ...] = (),
        on_state_change: Any = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._verify_ssl = verify_ssl
        self.exposed_domains = frozenset(exposed_domains) if exposed_domains else frozenset()
        self._on_state_change = on_state_change
        self._http: Any = None
        self._entities: dict[str, HAEntity] = {}
        self._ws_task: asyncio.Task[None] | None = None
        self._connected = False
        self._last_sync = 0.0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    # -------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        import httpx

        if not self.url or not self._token:
            raise IntegrationError("home assistant", "url and token are required")

        self._http = httpx.AsyncClient(
            base_url=f"{self.url}/api",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            timeout=15.0,
            verify=self._verify_ssl,
        )
        info = await self._get("/")
        log.info("home_assistant_connected", message=info.get("message", ""))
        await self.sync_states()
        self._ws_task = asyncio.create_task(self._websocket_loop())
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ state

    async def sync_states(self) -> int:
        """Full state pull; seeds the cache and repairs it after a reconnect."""
        states = await self._get("/states")
        self._entities.clear()
        for raw in states:
            entity = HAEntity(
                entity_id=raw["entity_id"],
                state=raw.get("state", ""),
                attributes=raw.get("attributes", {}),
                last_changed=raw.get("last_changed", ""),
            )
            if self.exposed_domains and entity.domain not in self.exposed_domains:
                continue
            self._entities[entity.entity_id] = entity
        self._last_sync = time.time()
        log.info("home_assistant_synced", entities=len(self._entities))
        return len(self._entities)

    def entities(self, domain: str = "") -> list[HAEntity]:
        values = list(self._entities.values())
        if domain:
            values = [e for e in values if e.domain == domain]
        return sorted(values, key=lambda e: e.friendly_name.lower())

    def get(self, entity_id: str) -> HAEntity | None:
        return self._entities.get(entity_id)

    def areas(self) -> list[str]:
        """Every distinct area (room) the exposed entities belong to."""
        return sorted({e.area for e in self._entities.values() if e.area})

    def in_area(self, area: str, *, domains: tuple[str, ...] = ()) -> list[HAEntity]:
        """Entities in a room, matched fuzzily and optionally filtered by domain.

        "living room" matches the area "Living Room"; an exact area name wins,
        but a substring ("bedroom" for "Main Bedroom") still resolves so spoken
        room names do not have to be exact.
        """
        needle = area.strip().lower()
        if not needle:
            return []
        exact = [e for e in self._entities.values() if e.area.lower() == needle]
        pool = exact or [e for e in self._entities.values() if e.area and needle in e.area.lower()]
        if domains:
            pool = [e for e in pool if e.domain in domains]
        return sorted(pool, key=lambda e: e.friendly_name.lower())

    def resolve(self, text: str, *, domain: str = "") -> list[HAEntity]:
        """Find entities matching a spoken description, best match first."""
        needle = text.strip().lower()
        if not needle:
            return []

        candidates = self.entities(domain)
        exact = [e for e in candidates if e.entity_id.lower() == needle]
        if exact:
            return exact

        scored: list[tuple[int, HAEntity]] = []
        for entity in candidates:
            name = entity.friendly_name.lower()
            entity_name = entity.entity_id.split(".", 1)[-1].replace("_", " ").lower()
            if name == needle or entity_name == needle:
                scored.append((100, entity))
            elif needle in name or needle in entity_name:
                # Prefer the shortest match: "kitchen light" over "kitchen light sensor".
                scored.append((80 - len(name), entity))
            elif all(word in name for word in needle.split()):
                scored.append((60 - len(name), entity))
            elif entity.area and needle in entity.area.lower():
                scored.append((40, entity))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entity for _, entity in scored[:10]]

    def resolve_one(self, text: str, *, domain: str = "") -> HAEntity:
        matches = self.resolve(text, domain=domain)
        if not matches:
            raise IntegrationError("home assistant", f"I can't find anything called '{text}'")
        if len(matches) > 1 and matches[0].friendly_name.lower() != text.strip().lower():
            names = ", ".join(e.friendly_name for e in matches[:4])
            if len({e.friendly_name for e in matches[:4]}) > 1:
                raise IntegrationError("home assistant", f"'{text}' could mean: {names}")
        return matches[0]

    # ------------------------------------------------------------------ camera

    async def camera_snapshot_jpeg(self, entity_id: str) -> bytes | None:
        """Fetch one current JPEG frame from a camera entity.

        A snapshot, not a live stream — plenty to see who is at the door
        without the bridge having to proxy continuous video, and it works
        the same way regardless of what actually backs the camera in HA.
        """
        if self._http is None:
            return None
        try:
            response = await self._http.get(f"/camera_proxy/{entity_id}")
        except Exception as exc:  # noqa: BLE001 - a stalled camera should not break anything else
            log.warning("camera_snapshot_failed", entity_id=entity_id, error=str(exc))
            return None
        if response.status_code != 200:
            return None
        return response.content

    # ---------------------------------------------------------------- commands

    async def call_service(
        self, domain: str, service: str, entity_id: str = "", **data: Any
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = dict(data)
        if entity_id:
            entity_id = self._resolve_entity_id(entity_id)
            payload["entity_id"] = entity_id
        result = await self._post(f"/services/{domain}/{service}", payload)
        log.info("ha_service_called", domain=domain, service=service, entity=entity_id)
        return result if isinstance(result, list) else []

    def _resolve_entity_id(self, entity_id: str) -> str:
        """Guard against a hallucinated id reaching Home Assistant.

        Home Assistant's service API does not error on an entity id that
        matches nothing — it just does nothing and returns 200. That makes a
        model-invented id (built from a mis-heard name, say) indistinguishable
        from success: the log says the call went through, nothing in the house
        moved, and there is no exception to catch. Real callers inside this
        class always pass an id straight from ``resolve_one``, which is already
        exact, so this only ever does work for a guessed one — and turns a
        silent no-op into the same "I can't find anything called" error normal
        name resolution gives.
        """
        if entity_id in self._entities:
            return entity_id
        return self.resolve_one(entity_id).entity_id

    async def turn_on(self, entity_id: str, **data: Any) -> str:
        domain = entity_id.split(".", 1)[0]
        await self.call_service(
            domain if domain in TOGGLEABLE else "homeassistant", "turn_on", entity_id, **data
        )
        return f"turned on {self._name(entity_id)}"

    async def turn_off(self, entity_id: str) -> str:
        domain = entity_id.split(".", 1)[0]
        await self.call_service(
            domain if domain in TOGGLEABLE else "homeassistant", "turn_off", entity_id
        )
        return f"turned off {self._name(entity_id)}"

    async def set_many(self, entity_ids: list[str], *, on: bool) -> int:
        """Turn a group of entities on or off in a single service call.

        Uses the domain-agnostic ``homeassistant.turn_on``/``turn_off``, which
        dispatches each id to its own domain — so a mixed list of lights and
        switches for one room is one round trip, not one per device.
        """
        ids = [e for e in entity_ids if e in self._entities]
        if not ids:
            return 0
        service = "turn_on" if on else "turn_off"
        # Posted directly rather than through call_service, which resolves a
        # single id; here the payload's entity_id is a list HA fans out itself.
        await self._post(f"/services/homeassistant/{service}", {"entity_id": ids})
        log.info("ha_set_many", service=service, count=len(ids))
        return len(ids)

    async def set_brightness(self, entity_id: str, percent: int) -> str:
        percent = max(0, min(100, percent))
        if percent == 0:
            return await self.turn_off(entity_id)
        await self.call_service("light", "turn_on", entity_id, brightness_pct=percent)
        return f"set {self._name(entity_id)} to {percent}%"

    async def set_temperature(self, entity_id: str, temperature: float) -> str:
        await self.call_service("climate", "set_temperature", entity_id, temperature=temperature)
        return f"set {self._name(entity_id)} to {temperature}°"

    async def activate_scene(self, entity_id: str) -> str:
        await self.call_service("scene", "turn_on", entity_id)
        return f"activated {self._name(entity_id)}"

    async def trigger_automation(self, entity_id: str) -> str:
        await self.call_service("automation", "trigger", entity_id)
        return f"triggered {self._name(entity_id)}"

    def _name(self, entity_id: str) -> str:
        entity = self._entities.get(entity_id)
        return entity.friendly_name if entity else entity_id

    # --------------------------------------------------------------- websocket

    async def _websocket_loop(self) -> None:
        """Keep a state subscription alive, reconnecting with backoff."""
        import websockets

        ws_url = (
            self.url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        )
        attempt = 0
        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=30) as socket:
                    await self._websocket_session(socket)
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnection is the normal path
                attempt += 1
                delay = min(60, 2**attempt)
                log.warning("ha_websocket_lost", error=str(exc)[:160], retry_in=delay)
                self._connected = False
                await asyncio.sleep(delay)

    async def _websocket_session(self, socket: Any) -> None:
        await socket.recv()  # auth_required
        await socket.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth = json.loads(await socket.recv())
        if auth.get("type") != "auth_ok":
            raise IntegrationError("home assistant", "websocket authentication failed")

        await socket.send(
            json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
        )
        self._connected = True
        log.info("ha_websocket_subscribed")

        # The cache can drift while the socket was down; resync on (re)connect.
        await self.sync_states()

        async for raw in socket:
            message = json.loads(raw)
            if message.get("type") != "event":
                continue
            data = message.get("event", {}).get("data", {})
            new_state = data.get("new_state")
            if not new_state:
                continue
            entity = HAEntity(
                entity_id=new_state["entity_id"],
                state=new_state.get("state", ""),
                attributes=new_state.get("attributes", {}),
                last_changed=new_state.get("last_changed", ""),
            )
            if self.exposed_domains and entity.domain not in self.exposed_domains:
                continue
            previous = self._entities.get(entity.entity_id)
            self._entities[entity.entity_id] = entity
            if (
                self._on_state_change is not None
                and previous is not None
                and previous.state != entity.state
            ):
                self._on_state_change(entity, previous)

    # ------------------------------------------------------------------- http

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._request("POST", path, payload)

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            raise IntegrationError("home assistant", "not connected")
        import httpx

        try:
            response = await self._http.request(method, path, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _http_detail(exc.response.status_code)
            raise IntegrationError("home assistant", detail) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("home assistant", f"could not reach {self.url}: {exc}") from exc
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}


def _http_detail(status: int) -> str:
    if status == 401:
        return "the access token was rejected — create a long-lived token in your profile"
    if status == 404:
        return "endpoint not found — check the server URL"
    return f"request failed with HTTP {status}"
