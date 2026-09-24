"""Skill and tool primitives.

A *skill* is a module of related capabilities (lights, Docker, the calendar). A
*tool* is one callable the language model may invoke. Tools are declared by
decorating an async method; the JSON schema the model sees is derived from the
method's type hints, so the signature and the schema cannot drift apart.

    class LightsSkill(Skill):
        name = "lights"

        @tool("Turn a light on or off.")
        async def set_light(
            self,
            entity: Annotated[str, Param("Entity id or friendly name")],
            state: Annotated[Literal["on", "off"], Param("Desired state")],
        ) -> str:
            ...

Tools that change the world declare ``mutating=True``; tools that could lose
data or interrupt a service declare ``destructive=True``, which routes them
through the confirmation flow before they ever run.
"""

from __future__ import annotations

import enum
import inspect
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args, get_origin

from ..runtime.logging import get_logger

if TYPE_CHECKING:
    from ..context import NovaContext

log = get_logger(__name__)

_PRIMITIVES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


@dataclass(slots=True, frozen=True)
class Param:
    """Annotation metadata for a tool parameter."""

    description: str = ""
    examples: tuple[str, ...] = ()
    enum: tuple[str, ...] = ()


@dataclass(slots=True)
class ToolSpec:
    """Everything the registry and the model need to know about one tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    skill: str = ""
    mutating: bool = False
    destructive: bool = False
    #: Free-form label used in the UI ("Home", "Server", "Calendar").
    category: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.skill}_{self.name}" if self.skill else self.name

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.qualified_name,
            "skill": self.skill,
            "description": self.description,
            "mutating": self.mutating,
            "destructive": self.destructive,
            "category": self.category,
        }


def tool(
    description: str,
    *,
    name: str | None = None,
    mutating: bool = False,
    destructive: bool = False,
    category: str = "",
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Mark an async method as a model-callable tool."""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool '{fn.__name__}' must be an async function")
        fn.__nova_tool__ = {  # type: ignore[attr-defined]
            "description": description,
            "name": name or fn.__name__,
            "mutating": mutating or destructive,
            "destructive": destructive,
            "category": category,
        }
        return fn

    return decorator


# ------------------------------------------------------------ schema building


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        optional = len(args) != len(get_args(annotation))
        if len(args) == 1:
            return args[0], optional
        return args[0], optional  # first arm wins; unions of many types are discouraged
    return annotation, False


def _json_type(annotation: Any, meta: Param | None) -> dict[str, Any]:
    annotation, _ = _unwrap_optional(annotation)
    origin = get_origin(annotation)

    if origin is Literal:
        values = [str(v) for v in get_args(annotation)]
        return {"type": "string", "enum": values}
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return {"type": "string", "enum": [str(e.value) for e in annotation]}
    if origin in (list, set, tuple):
        args = get_args(annotation)
        item = _json_type(args[0], None) if args else {"type": "string"}
        return {"type": "array", "items": item}
    if annotation is dict or origin is dict:
        return {"type": "object", "additionalProperties": True}
    if annotation in _PRIMITIVES:
        schema: dict[str, Any] = {"type": _PRIMITIVES[annotation]}
        if meta and meta.enum:
            schema["enum"] = list(meta.enum)
        return schema
    # Unknown annotation: accept a string and let the tool validate.
    return {"type": "string"}


def build_parameter_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """Derive a JSON Schema object from a tool method's signature."""
    signature = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001 - a forward ref we cannot resolve
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, parameter in signature.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue

        annotation = hints.get(param_name, parameter.annotation)
        meta: Param | None = None
        if get_origin(annotation) is Annotated:
            args = get_args(annotation)
            annotation = args[0]
            meta = next((a for a in args[1:] if isinstance(a, Param)), None)

        schema = _json_type(annotation, meta)
        if meta and meta.description:
            schema["description"] = meta.description
        if meta and meta.examples:
            schema["examples"] = list(meta.examples)
        properties[param_name] = schema

        if parameter.default is inspect.Parameter.empty:
            required.append(param_name)
        else:
            default = parameter.default
            if isinstance(default, (str, int, float, bool)):
                schema["default"] = default

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# --------------------------------------------------------------------- skills


@dataclass(slots=True)
class SkillInfo:
    name: str
    description: str
    available: bool
    reason: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)


class Skill:
    """Base class for a module of capabilities.

    Subclasses set :attr:`name` and :attr:`description`, then declare tools with
    the :func:`tool` decorator. Override :meth:`setup` for one-time preparation
    and :meth:`is_available` to hide the skill when its backing service is not
    configured — an unavailable skill's tools are never shown to the model, which
    keeps the prompt small and stops it from promising things it cannot do.
    """

    name: str = "skill"
    description: str = ""
    #: UI grouping label.
    category: str = "General"
    #: Extra guidance appended to the system prompt when this skill is active.
    prompt_hint: str = ""

    def __init__(self, ctx: NovaContext) -> None:
        self.ctx = ctx
        self.log = get_logger(f"nova.skill.{self.name}")

    async def setup(self) -> None:
        """Prepare the skill. Raise to disable it."""

    async def teardown(self) -> None:
        """Release anything acquired in :meth:`setup`."""

    def is_available(self) -> tuple[bool, str]:
        """Return ``(available, reason_if_not)``."""
        return True, ""

    def collect_tools(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for attr_name in dir(type(self)):
            if attr_name.startswith("__"):
                continue
            attr = getattr(type(self), attr_name, None)
            meta = getattr(attr, "__nova_tool__", None)
            if meta is None:
                continue
            bound = getattr(self, attr_name)
            specs.append(
                ToolSpec(
                    name=meta["name"],
                    description=meta["description"],
                    parameters=build_parameter_schema(attr),
                    handler=bound,
                    skill=self.name,
                    mutating=meta["mutating"],
                    destructive=meta["destructive"],
                    category=meta["category"] or self.category,
                )
            )
        specs.sort(key=lambda s: s.name)
        return specs

    def context_lines(self) -> list[str]:
        """Facts injected into the system prompt while this skill is available.

        Use sparingly — this runs on every turn and every line costs tokens.
        """
        return []
