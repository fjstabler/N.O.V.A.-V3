"""Adapters for the services in the spec.

Each one hits the smallest endpoint that proves the service is healthy and
returns a headline the assistant can speak. They are intentionally short —
the value is in having a uniform interface, not in wrapping every API surface.

To add a service: subclass :class:`ServiceAdapter`, implement :meth:`probe`, and
add it to :data:`ADAPTERS`. Nothing else changes.
"""

from __future__ import annotations

from typing import Any

from ...runtime.errors import IntegrationError
from .base import ServiceAdapter


class AdGuardAdapter(ServiceAdapter):
    """AdGuard Home — DNS filtering."""

    kind = "adguard"
    default_name = "AdGuard Home"

    def _auth(self) -> Any:
        import httpx

        return httpx.BasicAuth(self.username, self.password) if self.username else None

    async def probe(self) -> tuple[str, dict[str, Any]]:
        stats = await self.get("/control/stats", auth=self._auth())
        queries = int(stats.get("num_dns_queries", 0))
        blocked = int(stats.get("num_blocked_filtering", 0))
        percent = (blocked / queries * 100) if queries else 0.0
        return (
            f"{queries:,} queries today, {blocked:,} blocked ({percent:.0f}%)",
            {"queries": queries, "blocked": blocked, "blockedPercent": round(percent, 1)},
        )

    async def set_protection(self, enabled: bool) -> str:
        await self.post("/control/protection", json={"enabled": enabled}, auth=self._auth())
        return f"filtering {'enabled' if enabled else 'disabled'}"

    async def top_blocked(self, limit: int = 5) -> list[str]:
        stats = await self.get("/control/stats", auth=self._auth())
        entries = stats.get("top_blocked_domains", [])[:limit]
        return [f"{k} ({v})" for entry in entries for k, v in entry.items()]


class UptimeKumaAdapter(ServiceAdapter):
    """Uptime Kuma — read via the Prometheus metrics endpoint.

    Kuma's main API is Socket.IO, which is a heavy dependency for a status read.
    The ``/metrics`` endpoint is plain text, needs only an API key, and carries
    exactly what we want: per-monitor up/down.
    """

    kind = "uptime-kuma"
    default_name = "Uptime Kuma"

    def headers(self) -> dict[str, str]:
        return {"Accept": "text/plain"}

    async def probe(self) -> tuple[str, dict[str, Any]]:
        import httpx

        auth = httpx.BasicAuth("", self.api_key) if self.api_key else None
        text = await self.get("/metrics", auth=auth)
        monitors = _parse_kuma_metrics(text if isinstance(text, str) else "")
        if not monitors:
            return "reachable, no monitors reported", {}
        down = [name for name, up in monitors.items() if not up]
        summary = (
            f"all {len(monitors)} monitors up"
            if not down
            else f"{len(down)} of {len(monitors)} down: {', '.join(down[:4])}"
        )
        return summary, {"total": len(monitors), "down": down}


class JellyfinAdapter(ServiceAdapter):
    """Jellyfin — media server."""

    kind = "jellyfin"
    default_name = "Jellyfin"

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Emby-Token": self.api_key,
            "Authorization": f'MediaBrowser Client="NOVA", Device="NOVA", DeviceId="nova-core", '
            f'Version="3.0.0", Token="{self.api_key}"',
        }

    async def probe(self) -> tuple[str, dict[str, Any]]:
        info = await self.get("/System/Info")
        sessions = await self.get("/Sessions")
        playing = (
            [s for s in sessions if s.get("NowPlayingItem")] if isinstance(sessions, list) else []
        )
        summary = f"version {info.get('Version', '?')}"
        if playing:
            titles = ", ".join(s["NowPlayingItem"].get("Name", "?") for s in playing[:3])
            summary += f", {len(playing)} streaming: {titles}"
        else:
            summary += ", nothing playing"
        return summary, {"version": info.get("Version", ""), "activeStreams": len(playing)}

    async def now_playing(self) -> list[str]:
        sessions = await self.get("/Sessions")
        if not isinstance(sessions, list):
            return []
        return [
            f"{s.get('UserName', 'someone')} is watching "
            f"{s['NowPlayingItem'].get('Name', 'something')}"
            for s in sessions
            if s.get("NowPlayingItem")
        ]


class PlexAdapter(ServiceAdapter):
    """Plex Media Server."""

    kind = "plex"
    default_name = "Plex"

    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "X-Plex-Token": self.api_key}

    async def probe(self) -> tuple[str, dict[str, Any]]:
        identity = await self.get("/identity")
        container = (identity or {}).get("MediaContainer", {})
        sessions = await self.get("/status/sessions")
        media = (sessions or {}).get("MediaContainer", {})
        count = int(media.get("size", 0))
        summary = f"version {container.get('version', '?')}, {count} active stream(s)"
        return summary, {"version": container.get("version", ""), "activeStreams": count}

    async def now_playing(self) -> list[str]:
        sessions = await self.get("/status/sessions")
        items = (sessions or {}).get("MediaContainer", {}).get("Metadata", []) or []
        return [f"{i.get('grandparentTitle') or ''} {i.get('title', '')}".strip() for i in items]


class ImmichAdapter(ServiceAdapter):
    """Immich — photo library."""

    kind = "immich"
    default_name = "Immich"

    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "x-api-key": self.api_key}

    async def probe(self) -> tuple[str, dict[str, Any]]:
        stats = await self.get("/api/server-info/statistics")
        photos = int(stats.get("photos", 0))
        videos = int(stats.get("videos", 0))
        usage_gb = float(stats.get("usage", 0)) / 1024**3
        return (
            f"{photos:,} photos and {videos:,} videos, {usage_gb:.1f} GB",
            {"photos": photos, "videos": videos, "usageGb": round(usage_gb, 1)},
        )


class PortainerAdapter(ServiceAdapter):
    """Portainer — container management."""

    kind = "portainer"
    default_name = "Portainer"

    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "X-API-Key": self.api_key}

    async def probe(self) -> tuple[str, dict[str, Any]]:
        endpoints = await self.get("/api/endpoints")
        if not isinstance(endpoints, list):
            raise IntegrationError(self.name, "unexpected response")
        running = sum(
            int((e.get("Snapshots") or [{}])[0].get("RunningContainerCount", 0)) for e in endpoints
        )
        stopped = sum(
            int((e.get("Snapshots") or [{}])[0].get("StoppedContainerCount", 0)) for e in endpoints
        )
        return (
            f"{len(endpoints)} environment(s), {running} containers running, {stopped} stopped",
            {"environments": len(endpoints), "running": running, "stopped": stopped},
        )


class NodeRedAdapter(ServiceAdapter):
    """Node-RED — flow automation."""

    kind = "node-red"
    default_name = "Node-RED"

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Node-RED-API-Version": "v2"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def probe(self) -> tuple[str, dict[str, Any]]:
        settings = await self.get("/settings")
        flows = await self.get("/flows")
        tabs = (
            [f for f in flows if isinstance(f, dict) and f.get("type") == "tab"]
            if isinstance(flows, list)
            else []
        )
        return (
            f"version {settings.get('version', '?')}, {len(tabs)} flow(s)",
            {"version": settings.get("version", ""), "flows": len(tabs)},
        )


class HomepageAdapter(ServiceAdapter):
    """Homepage — the dashboard itself; a reachability check is enough."""

    kind = "homepage"
    default_name = "Homepage"

    def headers(self) -> dict[str, str]:
        return {"Accept": "text/html"}

    async def probe(self) -> tuple[str, dict[str, Any]]:
        await self.get("/")
        return "dashboard reachable", {}


class GenericAdapter(ServiceAdapter):
    """Any HTTP service — an up/down check by URL."""

    kind = "generic"
    default_name = "Service"

    def headers(self) -> dict[str, str]:
        return {"Accept": "*/*"}

    async def probe(self) -> tuple[str, dict[str, Any]]:
        await self.get("/")
        return "reachable", {}


#: Registry consulted when building adapters from configuration.
ADAPTERS: dict[str, type[ServiceAdapter]] = {
    "adguard": AdGuardAdapter,
    "uptime-kuma": UptimeKumaAdapter,
    "jellyfin": JellyfinAdapter,
    "plex": PlexAdapter,
    "immich": ImmichAdapter,
    "portainer": PortainerAdapter,
    "node-red": NodeRedAdapter,
    "homepage": HomepageAdapter,
    "generic": GenericAdapter,
}


def build_adapter(config: Any) -> ServiceAdapter:
    """Instantiate the adapter described by a :class:`HomeLabService` entry."""
    adapter_cls = ADAPTERS.get(config.kind, GenericAdapter)
    return adapter_cls(
        name=config.name or adapter_cls.default_name,
        url=config.url,
        api_key=config.api_key,
        username=config.username,
        password=config.password,
        verify_ssl=config.verify_ssl,
    )


def _parse_kuma_metrics(text: str) -> dict[str, bool]:
    """Extract ``monitor_status{monitor_name="x",...} 1`` lines."""
    monitors: dict[str, bool] = {}
    for line in text.splitlines():
        if not line.startswith("monitor_status{"):
            continue
        try:
            labels, _, value = line.rpartition(" ")
            name_key = 'monitor_name="'
            start = labels.index(name_key) + len(name_key)
            name = labels[start : labels.index('"', start)]
            monitors[name] = float(value) == 1.0
        except (ValueError, IndexError):
            continue
    return monitors
