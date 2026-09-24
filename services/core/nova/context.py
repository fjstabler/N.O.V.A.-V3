"""The dependency container handed to every service and skill.

Services receive the context rather than importing each other. Cross-subsystem
access goes through :meth:`service` / :meth:`require`, which return ``None`` (or
raise a typed error) when a capability is degraded — so a skill that needs
Docker fails with a clear message instead of an ``AttributeError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from .config import NovaPaths, NovaSettings, SettingsStore
from .runtime import EventBus, Service, ServiceManager, StateMachine
from .runtime.errors import DegradedCapability

if TYPE_CHECKING:
    from .skills.registry import SkillRegistry

T = TypeVar("T", bound=Service)


class NovaContext:
    """Shared runtime state. Constructed once, in :mod:`nova.app`."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self.store = store or SettingsStore()
        self.paths: NovaPaths = self.store.paths
        self.bus = EventBus()
        self.state = StateMachine(self.bus)
        self.services = ServiceManager(self.bus)
        self.skills: SkillRegistry | None = None
        #: Set once the process has begun shutting down.
        self.shutting_down = False

    @property
    def settings(self) -> NovaSettings:
        """Always read through this — the object is replaced on every patch."""
        return self.store.settings

    # --------------------------------------------------------------- services

    def service(self, name: str, kind: type[T] | None = None) -> T | None:
        """Return a *running* service, or ``None`` if absent or degraded."""
        svc = self.services.get(name)
        if svc is None or not svc.running:
            return None
        if kind is not None and not isinstance(svc, kind):
            return None
        return svc  # type: ignore[return-value]

    def require(self, name: str, kind: type[T] | None = None) -> T:
        """Return a running service or raise a user-legible error."""
        svc = self.service(name, kind)
        if svc is None:
            registered = self.services.get(name)
            reason = (
                registered.health.detail or registered.health.state.value
                if registered is not None
                else "not registered"
            )
            raise DegradedCapability(name, reason)
        return svc
