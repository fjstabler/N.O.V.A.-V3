"""Wire protocol between the core service and the desktop shell.

A single envelope shape carries four kinds of message::

    { "v": 1, "kind": "event", "topic": "state.changed", "id": "...",
      "ts": 1.7e9, "payload": { ... } }

``request``/``response``/``error`` are correlated by ``id``; ``event`` is
one-way, core → UI. Keeping one shape means the UI has exactly one parser and
one place to enforce the version check.

The TypeScript mirror of this file lives in ``packages/protocol`` and is kept in
sync by ``scripts/check_protocol.py``, which fails CI if the two drift.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = 1


class Kind(StrEnum):
    EVENT = "event"
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"
    HELLO = "hello"


@dataclass(slots=True)
class Message:
    kind: Kind
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    v: int = PROTOCOL_VERSION

    def encode(self) -> str:
        return json.dumps(
            {
                "v": self.v,
                "kind": self.kind.value,
                "topic": self.topic,
                "id": self.id,
                "ts": round(self.ts, 3),
                "payload": self.payload,
            },
            separators=(",", ":"),
            default=_fallback,
        )

    @classmethod
    def decode(cls, raw: str | bytes) -> Message:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("message must be a JSON object")
        version = int(data.get("v", PROTOCOL_VERSION))
        if version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version {version}")
        try:
            kind = Kind(data["kind"])
            topic = str(data["topic"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"malformed message: {exc}") from exc
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        return cls(
            kind=kind,
            topic=topic,
            payload=payload,
            id=str(data.get("id") or uuid.uuid4().hex),
            ts=float(data.get("ts") or time.time()),
            v=version,
        )

    # ----------------------------------------------------------- constructors

    @classmethod
    def event(cls, topic: str, payload: dict[str, Any] | None = None) -> Message:
        return cls(Kind.EVENT, topic, payload or {})

    @classmethod
    def response(
        cls, request_id: str, topic: str, payload: dict[str, Any] | None = None
    ) -> Message:
        return cls(Kind.RESPONSE, topic, payload or {}, id=request_id)

    @classmethod
    def error(cls, request_id: str, topic: str, code: str, message: str, **extra: Any) -> Message:
        return cls(Kind.ERROR, topic, {"code": code, "message": message, **extra}, id=request_id)


def _fallback(obj: Any) -> Any:
    """Last-resort JSON encoder for values a skill returned verbatim."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "as_payload"):
        return obj.as_payload()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


class Requests(StrEnum):
    """Topics the UI may send. Anything else is rejected."""

    HELLO = "hello"
    SETTINGS_GET = "settings.get"
    SETTINGS_SCHEMA = "settings.schema"
    SETTINGS_SET = "settings.set"
    VOICE_ACTIVATE = "voice.activate"
    VOICE_CANCEL = "voice.cancel"
    TEXT_SUBMIT = "text.submit"
    VOICE_AUDIO_SUBMIT = "voice.audio.submit"
    CONFIRM = "action.confirm"
    METRICS_GET = "system.metrics.get"
    SERVICES_GET = "system.services.get"
    SKILLS_GET = "skills.list"
    AUDIO_DEVICES = "audio.devices"
    AUDIO_SOURCE_ATTACH = "audio.source.attach"
    AUDIO_SOURCE_DETACH = "audio.source.detach"
    AUDIO_SOURCE_FRAME = "audio.source.frame"
    NOTIFICATION_DISMISS = "notification.dismiss"
    HOME_ENTITIES = "home.entities"
    HOMELAB_STATUS = "homelab.status"
    CALENDAR_AGENDA = "calendar.agenda"
    APP_QUIT = "app.quit"
