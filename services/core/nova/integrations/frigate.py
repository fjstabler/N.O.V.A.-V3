"""Frigate NVR, read through Home Assistant's entity list.

Frigate publishes each camera's detections to Home Assistant as entities N.O.V.A.
already mirrors over the WebSocket:

* ``binary_sensor.<camera>_<object>`` — ``on`` while that object (person, car…)
  is in view;
* ``sensor.<camera>_<object>`` (or ``…_count``) — how many are in view.

These are pure functions over the synced entity list, so "is anyone at the door"
is answered from Frigate's own on-device detection with no webcam guesswork — and
they test without a broker or a camera.
"""

from __future__ import annotations

from .homeassistant import HAEntity


def _object(object_type: str) -> str:
    return object_type.strip().lower()


def detection_sensors(entities: list[HAEntity], object_type: str = "person") -> list[HAEntity]:
    """The ``binary_sensor.<camera>_<object>`` occupancy sensors Frigate exposes."""
    suffix = f"_{_object(object_type)}"
    return [
        e for e in entities if e.domain == "binary_sensor" and e.entity_id.lower().endswith(suffix)
    ]


def count_sensors(entities: list[HAEntity], object_type: str = "person") -> list[HAEntity]:
    """The numeric ``sensor.<camera>_<object>`` counts, across Frigate versions."""
    obj = _object(object_type)
    return [
        e
        for e in entities
        if e.domain == "sensor"
        and (
            e.entity_id.lower().endswith(f"_{obj}") or e.entity_id.lower().endswith(f"_{obj}_count")
        )
    ]


def camera_name(entity_id: str, object_type: str = "person") -> str:
    """'binary_sensor.front_door_person' → 'front door'."""
    stem = entity_id.split(".", 1)[-1].lower()
    obj = _object(object_type)
    for suffix in (f"_{obj}_count", f"_{obj}"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.replace("_", " ").strip()


def _as_count(state: str) -> int | None:
    try:
        return int(float(state))
    except (ValueError, TypeError):
        return None


def active_cameras(
    entities: list[HAEntity], object_type: str = "person"
) -> list[tuple[str, int | None]]:
    """``(camera, count_or_None)`` for every camera currently seeing the object."""
    counts = {
        camera_name(e.entity_id, object_type): _as_count(e.state)
        for e in count_sensors(entities, object_type)
    }
    active: list[tuple[str, int | None]] = []
    for sensor in detection_sensors(entities, object_type):
        if sensor.state == "on":
            camera = camera_name(sensor.entity_id, object_type)
            active.append((camera, counts.get(camera)))
    return active


def is_detection_event(payload: dict[str, object], object_type: str = "person") -> bool:
    """True if a HOME_EVENT is a Frigate object sensor turning on (off → on)."""
    if payload.get("domain") != "binary_sensor":
        return False
    entity_id = str(payload.get("entityId", "")).lower()
    if not entity_id.endswith(f"_{_object(object_type)}"):
        return False
    return payload.get("state") == "on" and payload.get("previous") != "on"
