"""MQTT client.

paho-mqtt runs its network loop on its own thread, so every callback is bounced
back onto the event loop with ``call_soon_threadsafe`` before it touches
anything else in the process. Getting that boundary wrong is the classic source
of "works for an hour then deadlocks" in MQTT integrations.

Retained values are kept per topic so N.O.V.A. can answer "what's the greenhouse
humidity" from the last message rather than waiting for the next one.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..runtime.errors import IntegrationError, MissingDependency
from ..runtime.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Retained:
    topic: str
    payload: str
    received_at: float

    def decoded(self) -> Any:
        try:
            return json.loads(self.payload)
        except json.JSONDecodeError:
            return self.payload

    def as_payload(self) -> dict[str, Any]:
        return {"topic": self.topic, "value": self.decoded(), "receivedAt": self.received_at}


class MQTTClient:
    """Thin async wrapper over paho-mqtt."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        tls: bool = False,
        client_id: str = "nova-core",
        on_message: Callable[[str, str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._username = username
        self._password = password
        self._tls = tls
        self._client_id = client_id
        self._on_message = on_message
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = asyncio.Event()
        self.retained: dict[str, Retained] = {}
        self._subscriptions: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def connect(self, *, timeout: float = 10.0) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise MissingDependency("mqtt", "paho-mqtt", "home") from exc

        self._loop = asyncio.get_running_loop()
        # CallbackAPIVersion.VERSION2 is required by paho 2.x.
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id
            )
        except (AttributeError, TypeError):  # paho 1.x
            client = mqtt.Client(client_id=self._client_id)

        if self._username:
            if not self._tls:
                log.warning(
                    "mqtt_credentials_without_tls",
                    host=self.host,
                    detail="username/password will be sent unencrypted to this broker — "
                    "enable mqtt.tls if it supports it",
                )
            client.username_pw_set(self._username, self._password)
        if self._tls:
            client.tls_set()

        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        # Reconnect is handled by paho itself; bounded so a dead broker does not spin.
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        self._client = client
        try:
            await asyncio.to_thread(client.connect, self.host, self.port, 60)
        except OSError as exc:
            raise IntegrationError(
                "mqtt", f"could not reach {self.host}:{self.port} — {exc}"
            ) from exc
        client.loop_start()

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except TimeoutError as exc:
            client.loop_stop()
            raise IntegrationError(
                "mqtt", f"broker at {self.host} did not accept the connection"
            ) from exc
        log.info("mqtt_connected", host=self.host, port=self.port)

    # ------------------------------------------------- paho thread callbacks

    def _handle_connect(
        self, client: Any, userdata: Any, flags: Any, reason: Any, *args: Any
    ) -> None:
        code = getattr(reason, "value", reason)
        if code not in (0, None):
            log.warning("mqtt_connect_refused", code=code)
            return
        self._call_soon(self._connected.set)
        # Re-subscribe: a reconnect starts with a clean session.
        for topic in tuple(self._subscriptions):
            client.subscribe(topic)

    def _handle_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self._call_soon(self._connected.clear)
        log.warning("mqtt_disconnected", host=self.host)

    def _handle_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            payload = message.payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        record = Retained(message.topic, payload, time.time())
        self._call_soon(lambda: self.retained.__setitem__(message.topic, record))
        if self._on_message is not None:
            self._call_soon(lambda: self._on_message(message.topic, payload))  # type: ignore[misc]

    def _call_soon(self, fn: Callable[[], Any]) -> None:
        """Hop from paho's network thread onto the event loop."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn)

    # ------------------------------------------------------------- operations

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        if self._client is None:
            raise IntegrationError("mqtt", "not connected")
        self._subscriptions.add(topic)
        await asyncio.to_thread(self._client.subscribe, topic, qos)
        log.info("mqtt_subscribed", topic=topic)

    async def publish(
        self, topic: str, payload: Any, *, qos: int = 0, retain: bool = False
    ) -> None:
        if self._client is None:
            raise IntegrationError("mqtt", "not connected")
        body = payload if isinstance(payload, str) else json.dumps(payload)
        info = await asyncio.to_thread(self._client.publish, topic, body, qos, retain)
        if getattr(info, "rc", 0) != 0:
            raise IntegrationError("mqtt", f"publish to '{topic}' failed")
        log.debug("mqtt_published", topic=topic, bytes=len(body))

    def latest(self, topic: str) -> Retained | None:
        if topic in self.retained:
            return self.retained[topic]
        # Allow a suffix match so "greenhouse/humidity" finds "home/greenhouse/humidity".
        matches = [r for t, r in self.retained.items() if t.endswith(topic)]
        return max(matches, key=lambda r: r.received_at) if matches else None

    def topics(self) -> list[str]:
        return sorted(self.retained)

    async def close(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._connected.clear()
