"""Home lab: status and control of self-hosted services."""

from __future__ import annotations

from typing import Annotated

from ...integrations.homelab.adapters import (
    AdGuardAdapter,
    JellyfinAdapter,
    PlexAdapter,
)
from ...integrations.services import HomeLabService
from ..base import Param, Skill, tool


class HomeLabSkill(Skill):
    name = "homelab"
    description = (
        "Check and control self-hosted services: AdGuard, Uptime Kuma, "
        "Jellyfin, Plex, Immich, Portainer and Node-RED."
    )
    category = "Home Lab"

    def is_available(self) -> tuple[bool, str]:
        if self.ctx.service("homelab") is None:
            return False, "no home lab services configured"
        return True, ""

    @property
    def homelab(self) -> HomeLabService:
        return self.ctx.require("homelab", HomeLabService)

    def context_lines(self) -> list[str]:
        service = self.ctx.service("homelab", HomeLabService)
        if service is None or not service.statuses:
            return []
        offline = [s.name for s in service.statuses.values() if not s.online]
        names = ", ".join(sorted(service.adapters))
        line = f"Home lab services: {names}."
        if offline:
            line += f" Currently offline: {', '.join(offline)}."
        return [line]

    @tool("Check the status of every self-hosted service.")
    async def status_all(self) -> str:
        statuses = await self.homelab.refresh()
        if not statuses:
            return "No services configured."
        return "\n".join(s.describe() for s in statuses)

    @tool("Check one self-hosted service in detail.")
    async def status(
        self, name: Annotated[str, Param("Service name", examples=("jellyfin", "adguard"))]
    ) -> str:
        adapter = self.homelab.find(name)
        result = await adapter.status()
        return result.describe()

    @tool("List which self-hosted services are currently down.")
    async def offline_services(self) -> str:
        statuses = await self.homelab.refresh()
        offline = [s for s in statuses if not s.online]
        if not offline:
            return "Everything is up."
        return "\n".join(s.describe() for s in offline)

    @tool("Get what is currently playing on the media servers.")
    async def now_playing(self) -> str:
        lines: list[str] = []
        for adapter in self.homelab.adapters.values():
            if isinstance(adapter, (JellyfinAdapter, PlexAdapter)):
                try:
                    lines.extend(await adapter.now_playing())
                except Exception as exc:  # noqa: BLE001 - report per service
                    lines.append(f"{adapter.name}: unavailable ({exc})")
        return "\n".join(lines) if lines else "Nothing is playing."

    @tool("Get DNS filtering statistics from AdGuard Home.")
    async def dns_stats(self) -> str:
        for adapter in self.homelab.adapters.values():
            if isinstance(adapter, AdGuardAdapter):
                detail, _ = await adapter.probe()
                blocked = await adapter.top_blocked()
                text = f"AdGuard: {detail}."
                if blocked:
                    text += f" Most blocked: {', '.join(blocked)}."
                return text
        return "AdGuard Home is not configured."

    @tool("Enable or disable AdGuard DNS filtering.", destructive=True)
    async def set_dns_filtering(
        self, enabled: Annotated[bool, Param("True to enable filtering")]
    ) -> str:
        for adapter in self.homelab.adapters.values():
            if isinstance(adapter, AdGuardAdapter):
                return await adapter.set_protection(enabled)
        return "AdGuard Home is not configured."
