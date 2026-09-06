"""Loading, patching and persisting settings.

Precedence, lowest to highest: schema defaults → ``config.toml`` → environment
variables → runtime patches from the settings panel. Writes are atomic and the
file is chmod 0600, because it holds the OpenAI key and every integration token.

The store never hands raw secrets to the transport layer: :meth:`public_dict`
substitutes a sentinel, and :meth:`patch` treats that sentinel as "unchanged".
That way the settings panel can round-trip the whole document without ever
seeing — or accidentally erasing — a credential.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
import tomllib
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..runtime.errors import ConfigError
from ..runtime.logging import get_logger
from .paths import NovaPaths, get_paths
from .schema import REDACTED, NovaSettings

log = get_logger(__name__)

#: Environment overrides, e.g. NOVA_OPENAI__API_KEY=sk-...
ENV_PREFIX = "NOVA_"
ENV_NESTED_DELIM = "__"


def _iter_secret_paths(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    """Walk the schema and yield the dotted path of every secret field."""
    for name, field in model.model_fields.items():
        extra = field.json_schema_extra or {}
        if isinstance(extra, dict) and extra.get("secret"):
            yield (*prefix, name)
        annotation = field.annotation
        for candidate in _unwrap(annotation):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                yield from _iter_secret_paths(candidate, (*prefix, name))


def _unwrap(annotation: Any) -> Iterable[Any]:
    """Yield model classes reachable from a (possibly generic) annotation."""
    if annotation is None:
        return
    args = getattr(annotation, "__args__", None)
    if args:
        for arg in args:
            yield from _unwrap(arg)
    else:
        yield annotation


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``patch`` into ``base``; lists replace wholesale."""
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(raw: str) -> Any:
    """Best-effort typing for environment variable values."""
    lowered = raw.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    with contextlib.suppress(ValueError):
        return int(raw)
    with contextlib.suppress(ValueError):
        return float(raw)
    if raw.startswith(("[", "{")):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(raw)
    return raw


def _env_overrides() -> tuple[dict[str, Any], frozenset[str]]:
    """Return the override tree and the dotted paths it covers.

    The paths matter as much as the values. An environment variable silently
    outranking the settings panel is a genuinely nasty trap: the panel shows the
    stored value, saving appears to succeed, and the running assistant keeps
    using something else with nothing on screen to explain it. Callers use this
    set to say so out loud.
    """
    overrides: dict[str, Any] = {}
    paths: set[str] = set()
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX) or key == "NOVA_HOME":
            continue
        path = key[len(ENV_PREFIX) :].lower().split(ENV_NESTED_DELIM)
        cursor = overrides
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):  # conflicting override; ignore
                break
        else:
            cursor[path[-1]] = _coerce(value)
            paths.add(".".join(path))
    return overrides, frozenset(paths)


class SettingsStore:
    """Owns the live :class:`NovaSettings` document."""

    def __init__(self, paths: NovaPaths | None = None) -> None:
        self.paths = (paths or get_paths()).ensure()
        self._secret_paths = frozenset(_iter_secret_paths(NovaSettings))
        #: Dotted paths currently pinned by an environment variable. Anything
        #: listed here ignores whatever the settings panel writes.
        self.env_overrides: frozenset[str] = frozenset()
        #: The document as it exists on disk, before environment overrides.
        self._file_data: dict[str, Any] = {}
        self._listeners: list[Callable[[NovaSettings, dict[str, Any]], None]] = []
        self._settings = self._load()

    # ------------------------------------------------------------------ load

    def _load(self) -> NovaSettings:
        data: dict[str, Any] = {}
        if self.paths.config_file.exists():
            try:
                with self.paths.config_file.open("rb") as fh:
                    data = tomllib.load(fh)
            except (tomllib.TOMLDecodeError, OSError) as exc:
                raise ConfigError(
                    f"could not read {self.paths.config_file}", detail=str(exc)
                ) from exc

        # What the file says, kept apart from what the environment imposes on
        # top of it. Only this ever gets written back, so a value supplied via
        # the environment can never leak into config.toml.
        self._file_data = data
        env, self.env_overrides = _env_overrides()
        data = _deep_merge(data, env)
        if self.env_overrides:
            # Never log the values — several of these are credentials.
            log.info("settings_overridden_by_environment", paths=sorted(self.env_overrides))
        try:
            settings = NovaSettings.model_validate(data)
        except ValidationError as exc:
            # A bad hand-edit must not brick the assistant: fall back to defaults
            # for the offending sections and keep the file for the user to fix.
            log.error("invalid_config", errors=exc.error_count(), file=str(self.paths.config_file))
            settings = self._salvage(data, exc)

        if not settings.transport.token:
            settings.transport.token = secrets.token_urlsafe(32)
            self._write(settings)
        return settings

    def _salvage(self, data: dict[str, Any], error: ValidationError) -> NovaSettings:
        """Drop only the invalid sections rather than the whole document."""
        broken = {str(e["loc"][0]) for e in error.errors() if e.get("loc")}
        pruned = {k: v for k, v in data.items() if k not in broken}
        try:
            return NovaSettings.model_validate(pruned)
        except ValidationError:
            return NovaSettings()

    # ----------------------------------------------------------------- access

    @property
    def settings(self) -> NovaSettings:
        return self._settings

    def public_dict(self) -> dict[str, Any]:
        """Settings with secrets replaced by a sentinel — safe to send to the UI."""
        data = self._settings.model_dump(mode="json")
        for path in self._secret_paths:
            self._redact(data, path)
        return data

    def _redact(self, node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, list):
            for item in node:
                self._redact(item, path)
            return
        if not isinstance(node, dict):
            return
        head, *rest = path
        if not rest:
            if node.get(head):
                node[head] = REDACTED
            return
        child = node.get(head)
        if child is not None:
            self._redact(child, tuple(rest))

    def secret_field_paths(self) -> list[str]:
        return sorted(".".join(p) for p in self._secret_paths)

    # ------------------------------------------------------------------ write

    def patch(self, patch: dict[str, Any], *, persist: bool = True) -> NovaSettings:
        """Apply a partial update from the settings panel.

        Any value equal to the redaction sentinel is dropped so a round-tripped
        document cannot wipe stored credentials.
        """
        cleaned = self._strip_sentinels(patch)
        # The edit lands in the file document; the environment is then layered
        # back over it, so precedence is the same at runtime as it is at load.
        file_data = _deep_merge(self._file_data, cleaned)
        env, _ = _env_overrides()
        try:
            updated = NovaSettings.model_validate(_deep_merge(file_data, env))
        except ValidationError as exc:
            raise ConfigError("invalid settings", detail=_format_validation(exc)) from exc
        self._file_data = file_data

        previous = self._settings
        self._settings = updated
        if persist:
            self._write(updated)
        changed = _diff(previous.model_dump(mode="json"), updated.model_dump(mode="json"))
        shadowed = sorted(set(changed) & self.env_overrides)
        if shadowed:
            log.warning("settings_saved_but_shadowed_by_environment", paths=shadowed)
        for listener in tuple(self._listeners):
            with contextlib.suppress(Exception):
                listener(updated, changed)
        log.info("settings_updated", sections=sorted({k.split(".")[0] for k in changed}))
        return updated

    def _strip_sentinels(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {k: self._strip_sentinels(v) for k, v in node.items() if v != REDACTED}
        if isinstance(node, list):
            return [self._strip_sentinels(v) for v in node]
        return node

    def _document_for_disk(self, settings: NovaSettings) -> dict[str, Any]:
        """The full settings document, with environment values stripped out.

        Dumping the live settings would bake whatever the environment supplied
        into the file — which for `NOVA_OPENAI__API_KEY` means writing a secret
        the user deliberately kept out of it. Each overridden path is restored
        to whatever the file held, or removed so the schema default applies.
        """
        data = settings.model_dump(mode="json")
        for dotted in self.env_overrides:
            path = dotted.split(".")
            original = _get_path(self._file_data, path)
            if original is _MISSING:
                _del_path(data, path)
            else:
                _set_path(data, path, original)
        return data

    def _write(self, settings: NovaSettings) -> None:
        try:
            import tomlkit

            document = tomlkit.dumps(self._document_for_disk(settings))
        except ImportError:  # pragma: no cover - tomlkit is a hard dependency
            document = _minimal_toml(self._document_for_disk(settings))

        target = self.paths.config_file
        tmp = target.with_suffix(".toml.tmp")
        header = (
            "# N.O.V.A. configuration — written by the settings panel.\n"
            "# Hand edits are respected; invalid sections fall back to defaults.\n"
            "# This file contains credentials. Keep it readable only by you.\n\n"
        )
        try:
            tmp.write_text(header + document, encoding="utf-8")
            if sys.platform != "win32":
                tmp.chmod(0o600)
            tmp.replace(target)
        except OSError as exc:
            raise ConfigError(f"could not write {target}", detail=str(exc)) from exc

    def reload(self) -> NovaSettings:
        self._settings = self._load()
        return self._settings

    def on_change(
        self, listener: Callable[[NovaSettings, dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Subscribe to settings changes. Returns an unsubscribe callable."""
        self._listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return remove


class _Missing:
    """Sentinel distinguishing 'absent' from 'present and None'."""


_MISSING = _Missing()


def _get_path(data: dict[str, Any], path: list[str]) -> Any:
    cursor: Any = data
    for part in path:
        if not isinstance(cursor, dict) or part not in cursor:
            return _MISSING
        cursor = cursor[part]
    return cursor


def _set_path(data: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = data
    for part in path[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[path[-1]] = value


def _del_path(data: dict[str, Any], path: list[str]) -> None:
    cursor: Any = data
    for part in path[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return
        cursor = cursor[part]
    if isinstance(cursor, dict):
        cursor.pop(path[-1], None)


def _diff(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flat map of dotted-path → new value for everything that changed."""
    changed: dict[str, Any] = {}
    for key in set(before) | set(after):
        path = f"{prefix}{key}"
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changed.update(_diff(old, new, f"{path}."))
        elif old != new:
            changed[path] = new
    return changed


def _format_validation(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:6])


def _minimal_toml(data: dict[str, Any], prefix: str = "") -> str:  # pragma: no cover
    """Dependency-free TOML writer used only if tomlkit is missing."""
    scalars, tables = [], []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append(f"\n[{prefix}{key}]\n" + _minimal_toml(value, f"{prefix}{key}."))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            for item in value:
                tables.append(f"\n[[{prefix}{key}]]\n" + _minimal_toml(item, f"{prefix}{key}."))
        else:
            scalars.append(f"{key} = {json.dumps(value)}")
    return "\n".join(scalars) + "\n" + "".join(tables)


def default_config_path() -> Path:
    return get_paths().config_file
