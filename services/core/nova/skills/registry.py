"""Skill discovery, tool dispatch and the confirmation gate.

Discovery has three sources, in ascending order of trust required:

1. built-ins under :mod:`nova.skills.builtin` — always scanned;
2. installed packages advertising a ``nova.skills`` entry point;
3. ``.py`` files dropped into the user's plugin directory.

Adding a capability therefore never means editing this file, which is the whole
point of the plugin architecture: new modules are additive.

Dispatch enforces the safety model. A tool marked ``destructive`` does not run
on first call — it raises :class:`ConfirmationRequired` carrying a token, and
only a matching :meth:`confirm` actually executes it. Tokens are single-use and
expire, so a "yes" a minute later cannot reboot the server.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import pkgutil
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..context import NovaContext
from ..runtime import Service, Topics
from ..runtime.errors import ConfirmationRequired, FinalAnswer, SkillError, ToolExecutionError
from .base import Skill, SkillInfo, ToolSpec

#: How long a pending destructive action stays confirmable.
CONFIRMATION_TTL = 120.0


@dataclass(slots=True)
class _PendingAction:
    spec: ToolSpec
    arguments: dict[str, Any]
    summary: str
    created_at: float


class SkillRegistry(Service):
    """Owns every skill instance and every tool the model can call."""

    name = "skills"
    requires = ("memory",)

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self._skills: dict[str, Skill] = {}
        self._unavailable: dict[str, str] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._pending: dict[str, _PendingAction] = {}
        self._discovered: list[type[Skill]] = []

    # -------------------------------------------------------------- lifecycle

    async def on_start(self) -> None:
        self.ctx.skills = self
        disabled = set(self.ctx.settings.plugins.disabled)
        # Kept so re-evaluation can rediscover without a second directory scan.
        self._discovered = [c for c in self._discover() if c.name not in disabled]

        for skill_cls in self._discovered:
            await self._install(skill_cls)

        self.log.info(
            "skills_ready",
            available=len(self._skills),
            unavailable=len(self._unavailable),
            tools=len(self._tools),
        )

    async def reevaluate(self) -> set[str]:
        """Re-run availability for every discovered skill.

        `is_available()` is only ever checked at start, which means a skill
        whose backing service was unconfigured at boot never gets a second
        look — the service can reconnect (say, a Home Assistant token being
        saved) while the skill and its tools stay absent from the model's
        catalogue. Silently, since nothing in that path raises.

        Called whenever a service restarts, so "connect this integration" and
        "its tools appear" happen in the same instant instead of needing a
        restart of the whole process.

        Returns the set of skill names whose availability changed, so the
        caller can decide whether the change is worth telling the user about.
        """
        changed: set[str] = set()
        for skill_cls in self._discovered:
            was_available = skill_cls.name in self._skills
            if was_available:
                continue  # a skill that loaded stays loaded; setup is not repeated
            available, reason = self._probe(skill_cls)
            if available and skill_cls.name not in self._skills:
                await self._install(skill_cls)
                if skill_cls.name in self._skills:
                    changed.add(skill_cls.name)
            elif not available:
                self._unavailable[skill_cls.name] = reason
        return changed

    def _probe(self, skill_cls: type[Skill]) -> tuple[bool, str]:
        """Check availability without committing to a full install."""
        try:
            probe = skill_cls(self.ctx)
        except Exception as exc:  # noqa: BLE001 - a broken constructor is not availability
            return False, str(exc)
        return probe.is_available()

    async def _install(self, skill_cls: type[Skill]) -> None:
        try:
            skill = skill_cls(self.ctx)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not stop boot
            self.log.warning("skill_construct_failed", skill=skill_cls.name, error=str(exc))
            self._unavailable[skill_cls.name] = str(exc)
            return

        available, reason = skill.is_available()
        if not available:
            self._unavailable[skill.name] = reason
            self.log.debug("skill_unavailable", skill=skill.name, reason=reason)
            return

        try:
            await skill.setup()
        except Exception as exc:  # noqa: BLE001
            self._unavailable[skill.name] = str(exc)
            self.log.warning("skill_setup_failed", skill=skill.name, error=str(exc))
            return

        self._skills[skill.name] = skill
        for spec in skill.collect_tools():
            if spec.qualified_name in self._tools:
                self.log.warning("duplicate_tool", tool=spec.qualified_name)
                continue
            self._tools[spec.qualified_name] = spec

    async def on_stop(self) -> None:
        for skill in self._skills.values():
            try:
                await skill.teardown()
            except Exception:  # noqa: BLE001
                self.log.warning("skill_teardown_failed", skill=skill.name)
        self._skills.clear()
        self._tools.clear()

    def describe(self) -> str:
        return f"{len(self._skills)} skills · {len(self._tools)} tools"

    # -------------------------------------------------------------- discovery

    def _discover(self) -> list[type[Skill]]:
        found: list[type[Skill]] = []
        found.extend(self._discover_builtin())
        found.extend(self._discover_entry_points())
        if self.ctx.settings.plugins.allow_third_party:
            found.extend(self._discover_directories())

        seen: set[str] = set()
        unique: list[type[Skill]] = []
        for cls in found:
            if cls.name in seen:
                self.log.warning("duplicate_skill_name", skill=cls.name)
                continue
            seen.add(cls.name)
            unique.append(cls)
        return unique

    def _discover_builtin(self) -> list[type[Skill]]:
        from . import builtin

        classes: list[type[Skill]] = []
        for module_info in pkgutil.iter_modules(builtin.__path__):
            try:
                module = importlib.import_module(f"{builtin.__name__}.{module_info.name}")
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "builtin_skill_import_failed", module=module_info.name, error=str(exc)
                )
                continue
            classes.extend(_skill_classes_in(module))
        return classes

    def _discover_entry_points(self) -> list[type[Skill]]:
        from importlib.metadata import entry_points

        classes: list[type[Skill]] = []
        try:
            points = entry_points(group="nova.skills")
        except Exception:  # noqa: BLE001 - older importlib behaviour
            return classes
        for point in points:
            try:
                loaded = point.load()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("plugin_load_failed", plugin=point.name, error=str(exc))
                continue
            if inspect.isclass(loaded) and issubclass(loaded, Skill):
                classes.append(loaded)
            elif inspect.ismodule(loaded):
                classes.extend(_skill_classes_in(loaded))
        return classes

    def _discover_directories(self) -> list[type[Skill]]:
        roots = [self.ctx.paths.plugins_dir]
        roots.extend(Path(p).expanduser() for p in self.ctx.settings.plugins.search_paths)

        classes: list[type[Skill]] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                module = self._load_file(path)
                if module is not None:
                    classes.extend(_skill_classes_in(module))
        return classes

    def _load_file(self, path: Path) -> Any:
        module_name = f"nova_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self.log.info("plugin_loaded", path=str(path))
            return module
        except Exception as exc:  # noqa: BLE001
            self.log.warning("plugin_import_failed", path=str(path), error=str(exc))
            return None

    # ------------------------------------------------------------------ query

    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        return [spec.as_openai_tool() for spec in self._tools.values()]

    def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def prompt_context(self) -> list[str]:
        """Lines every active skill wants in the system prompt."""
        lines: list[str] = []
        for skill in self._skills.values():
            if skill.prompt_hint:
                lines.append(skill.prompt_hint)
            lines.extend(skill.context_lines())
        return lines

    def catalogue(self) -> list[SkillInfo]:
        infos = [
            SkillInfo(
                name=skill.name,
                description=skill.description,
                available=True,
                tools=[s.as_payload() for s in self._tools.values() if s.skill == skill.name],
            )
            for skill in self._skills.values()
        ]
        infos.extend(
            SkillInfo(name=name, description="", available=False, reason=reason)
            for name, reason in self._unavailable.items()
        )
        infos.sort(key=lambda i: (not i.available, i.name))
        return infos

    # --------------------------------------------------------------- dispatch

    async def call(
        self, name: str, arguments: dict[str, Any] | str, *, confirmed: bool = False
    ) -> Any:
        """Execute a tool by qualified name.

        ``arguments`` accepts the raw JSON string the model emits as well as a
        decoded mapping.
        """
        spec = self._tools.get(name)
        if spec is None:
            raise ToolExecutionError(name, "unknown tool")

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ToolExecutionError(name, f"arguments were not valid JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise ToolExecutionError(name, "arguments must be an object")

        arguments = self._filter_arguments(spec, arguments)

        if spec.destructive and not confirmed:
            raise self._require_confirmation(spec, arguments)

        return await self._invoke(spec, arguments)

    async def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        started = time.perf_counter()
        self.bus.publish(
            Topics.TOOL_STARTED,
            {"tool": spec.qualified_name, "category": spec.category, "arguments": arguments},
            source="skills",
        )
        try:
            result = await asyncio.wait_for(spec.handler(**arguments), timeout=120)
        except TypeError as exc:
            raise ToolExecutionError(spec.qualified_name, f"bad arguments: {exc}") from exc
        except TimeoutError as exc:
            raise ToolExecutionError(spec.qualified_name, "timed out after 120s") from exc
        except FinalAnswer:
            # Must pass through untouched. The broad clause below would turn it
            # into a ToolExecutionError whose message is the answer — which the
            # orchestrator then sends to the model as "Error: ...", putting in a
            # prompt precisely what raising it exists to keep out.
            raise
        except SkillError:
            raise
        except Exception as exc:
            raise ToolExecutionError(spec.qualified_name, str(exc)) from exc
        finally:
            elapsed = round((time.perf_counter() - started) * 1000)
            self.bus.publish(
                Topics.TOOL_FINISHED,
                {"tool": spec.qualified_name, "ms": elapsed},
                source="skills",
            )
        if self.ctx.settings.developer.trace_tool_calls:
            self.log.info(
                "tool_call", tool=spec.qualified_name, args=arguments, result=str(result)[:400]
            )
        return result

    def _filter_arguments(self, spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
        """Drop keys the tool does not accept and report missing required ones.

        Models occasionally invent an extra field; silently ignoring it beats
        failing the whole turn with a TypeError.
        """
        allowed = set(spec.parameters.get("properties", {}))
        extra = set(arguments) - allowed
        if extra:
            self.log.debug("tool_extra_arguments", tool=spec.qualified_name, extra=sorted(extra))
        filtered = {k: v for k, v in arguments.items() if k in allowed}
        missing = [k for k in spec.parameters.get("required", []) if k not in filtered]
        if missing:
            raise ToolExecutionError(spec.qualified_name, f"missing required: {', '.join(missing)}")
        return filtered

    # ----------------------------------------------------------- confirmation

    def _require_confirmation(
        self, spec: ToolSpec, arguments: dict[str, Any]
    ) -> ConfirmationRequired:
        self._expire_pending()
        token = secrets.token_urlsafe(12)
        summary = _summarise(spec, arguments)
        self._pending[token] = _PendingAction(spec, arguments, summary, time.time())
        # Token deliberately not logged: it is a short-lived bearer credential for
        # this one action, and the log file isn't guarded as tightly as a secret.
        self.log.info("confirmation_required", tool=spec.qualified_name)
        return ConfirmationRequired(spec.qualified_name, summary, token)

    async def confirm(self, token: str) -> Any:
        """Run a previously-gated destructive action."""
        self._expire_pending()
        pending = self._pending.pop(token, None)
        if pending is None:
            raise SkillError("that confirmation has expired — ask me again")
        return await self._invoke(pending.spec, pending.arguments)

    def cancel(self, token: str) -> bool:
        return self._pending.pop(token, None) is not None

    def latest_pending(self) -> tuple[str, _PendingAction] | None:
        """Most recent unconfirmed action — lets 'yes' resolve without a token."""
        self._expire_pending()
        if not self._pending:
            return None
        token = max(self._pending, key=lambda t: self._pending[t].created_at)
        return token, self._pending[token]

    def _expire_pending(self) -> None:
        cutoff = time.time() - CONFIRMATION_TTL
        for token in [t for t, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[token]


def _skill_classes_in(module: Any) -> list[type[Skill]]:
    return [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Skill) and obj is not Skill and obj.__module__ == module.__name__
    ]


def _summarise(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """A short, speakable description of what is about to happen."""
    if not arguments:
        return spec.description
    rendered = ", ".join(f"{k}={v}" for k, v in list(arguments.items())[:4])
    return f"{spec.description} ({rendered})"
