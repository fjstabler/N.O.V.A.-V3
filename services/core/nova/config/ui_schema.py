"""Derives the settings-panel layout from the pydantic schema.

The panel is data-driven: the UI asks for this descriptor and renders controls
from it. Adding a field to :mod:`nova.config.schema` makes it appear in the
panel with the right control type, bounds and help text — there is no second
place to update, and therefore no way for the two to drift.
"""

from __future__ import annotations

import types
import typing
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .schema import NovaSettings

#: Presentation order and human labels for the top-level sections.
SECTIONS: list[tuple[str, str, str]] = [
    ("assistant", "Assistant", "Identity, persona and locale"),
    ("openai", "Reasoning", "OpenAI — the everyday model"),
    ("anthropic", "Advanced reasoning", "Claude — coding, system control and complex requests"),
    ("voice", "Voice", "Wake word, transcription and synthesis"),
    ("appearance", "Appearance", "Theme, motion and rendering"),
    ("memory", "Memory", "What N.O.V.A. remembers, and for how long"),
    ("notifications", "Notifications", "Floating panels and quiet hours"),
    ("home_assistant", "Home Assistant", "Smart home control"),
    ("mqtt", "MQTT", "Message broker for devices and automations"),
    ("calendar", "Calendar", "CalDAV accounts and reminders"),
    ("server", "Server", "Ubuntu monitoring and administration"),
    ("desktop", "Desktop", "Open pages, files and apps on this machine"),
    ("homelab", "Home Lab", "Self-hosted services"),
    ("vision", "Vision", "Screen and camera understanding"),
    ("security", "Security", "Room-watch: face recognition alerts"),
    ("presence", "Presence", "Reach me by voice when I'm here, phone when I'm away"),
    ("plugins", "Plugins", "Skill modules"),
    ("developer", "Developer", "Diagnostics and debugging"),
    ("transport", "Connection", "Local bridge between UI and core"),
]

_CONTROL_BY_TYPE: dict[Any, str] = {
    bool: "toggle",
    int: "number",
    float: "number",
    str: "text",
}


def _unwrap_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _numeric_bounds(field: FieldInfo) -> dict[str, float]:
    bounds: dict[str, float] = {}
    for meta in field.metadata:
        for attr, key in (("ge", "min"), ("gt", "min"), ("le", "max"), ("lt", "max")):
            value = getattr(meta, attr, None)
            if value is not None:
                bounds[key] = float(value)
    return bounds


def _describe_field(name: str, field: FieldInfo, model: type[BaseModel]) -> dict[str, Any]:
    annotation = _unwrap_optional(field.annotation)
    origin = get_origin(annotation)
    extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}

    entry: dict[str, Any] = {
        "key": name,
        "label": _humanise(name),
        "help": field.description or "",
        "secret": bool(extra.get("secret")),
    }

    if origin is Literal:
        entry["control"] = "select"
        entry["options"] = [{"value": v, "label": _humanise(str(v))} for v in get_args(annotation)]
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        entry["control"] = "group"
        entry["fields"] = _describe_model(annotation)
    elif origin in (list, set, tuple):
        (item_type,) = get_args(annotation) or (str,)
        if isinstance(item_type, type) and issubclass(item_type, BaseModel):
            entry["control"] = "list-of-groups"
            entry["fields"] = _describe_model(item_type)
            entry["itemLabelKey"] = "name" if "name" in item_type.model_fields else None
        else:
            entry["control"] = "string-list"
    elif annotation in _CONTROL_BY_TYPE:
        control = _CONTROL_BY_TYPE[annotation]
        bounds = _numeric_bounds(field)
        # A bounded float reads better as a slider than a spinner.
        if annotation is float and {"min", "max"} <= bounds.keys():
            control = "slider"
            entry["step"] = _step_for(bounds["min"], bounds["max"])
        entry["control"] = control
        entry.update(bounds)
        if annotation is str and extra.get("secret"):
            entry["control"] = "password"
        elif annotation is str and name in ("persona",):
            entry["control"] = "textarea"
    else:
        entry["control"] = "text"

    if entry["control"] not in ("group", "list-of-groups") and not entry["secret"]:
        entry["default"] = _json_default(model, name)
    return entry


def _step_for(low: float, high: float) -> float:
    span = high - low
    if span <= 2:
        return 0.05
    if span <= 20:
        return 0.5
    return 1.0


def _json_default(model: type[BaseModel], name: str) -> Any:
    field = model.model_fields[name]
    if field.default_factory is not None:  # type: ignore[truthy-function]
        try:
            value = field.default_factory()  # type: ignore[misc]
        except TypeError:
            return None
    else:
        value = field.default
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value if isinstance(value, (str, int, float, bool, list, dict, type(None))) else None


def _describe_model(model: type[BaseModel]) -> list[dict[str, Any]]:
    return [
        _describe_field(name, field, model)
        for name, field in model.model_fields.items()
        if name != "version"
    ]


def _humanise(name: str) -> str:
    words = name.replace("-", " ").replace("_", " ").split()
    acronyms = {"api", "url", "id", "mqtt", "gpu", "cpu", "tts", "stt", "ssl", "vad", "fps", "ha"}
    return " ".join(w.upper() if w.lower() in acronyms else w.capitalize() for w in words)


def describe_settings() -> list[dict[str, Any]]:
    """Return the ordered section descriptors the settings panel renders."""
    fields = NovaSettings.model_fields
    described: list[dict[str, Any]] = []
    for key, label, blurb in SECTIONS:
        field = fields.get(key)
        if field is None:
            continue
        annotation = _unwrap_optional(field.annotation)
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            continue
        described.append(
            {
                "key": key,
                "label": label,
                "description": blurb,
                "fields": _describe_model(annotation),
            }
        )
    return described
