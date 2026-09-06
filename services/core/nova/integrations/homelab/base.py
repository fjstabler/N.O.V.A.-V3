"""Common shape for every self-hosted service adapter.

An adapter answers two questions: *is it up* and *what is it doing*. Adding a new
service means writing one subclass and registering it — the skill layer, the UI
and the status polling all work off this interface, so nothing above needs to
know what a Jellyfin is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ...runtime.errors import IntegrationError
from ...runtime.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ServiceStatus:
    name: str
    kind: str
    online: bool
    detail: str = ""
    latency_ms: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)
    url: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "online": self.online,
            "detail": self.detail,
            "latencyMs": self.latency_ms,
            "metrics": self.metrics,
            "checkedAt": self.checked_at,
            "url": self.url,
        }

    def describe(self) -> str:
        if not self.online:
            return f"{self.name} is offline ({self.detail or 'no response'})"
        text = f"{self.name} is online"
        if self.detail:
            text += f" — {self.detail}"
        return text


class ServiceAdapter:
    """Base adapter for one home lab service."""

    kind = "generic"
    #: Human label used when the user did not name the service.
    default_name = "Service"

    def __init__(
        self,
        *,
        name: str = "",
        url: str = "",
        api_key: str = "",
        username: str = "",
        password: str = "",
        verify_ssl: bool = True,
    ) -> None:
        self.name = name or self.default_name
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self._client: Any = None

    # ------------------------------------------------------------------ http

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.url,
                timeout=10.0,
                verify=self.verify_ssl,
                headers=self.headers(),
                follow_redirects=True,
            )
        return self._client

    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        try:
            response = await self._http().request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise IntegrationError(self.name, f"HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise IntegrationError(self.name, str(exc)) from exc
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return response.text

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    # -------------------------------------------------------------- interface

    async def status(self) -> ServiceStatus:
        """Reachability plus whatever headline numbers the service exposes."""
        started = time.perf_counter()
        try:
            detail, metrics = await self.probe()
            online = True
        except IntegrationError as exc:
            detail, metrics, online = exc.message.split(": ", 1)[-1], {}, False
        except Exception as exc:  # noqa: BLE001
            detail, metrics, online = str(exc)[:120], {}, False
        return ServiceStatus(
            name=self.name,
            kind=self.kind,
            online=online,
            detail=detail,
            latency_ms=int((time.perf_counter() - started) * 1000),
            metrics=metrics,
            url=self.url,
        )

    async def probe(self) -> tuple[str, dict[str, Any]]:
        """Return ``(summary, metrics)``. Raise to report the service as down."""
        await self.get("/")
        return "reachable", {}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
