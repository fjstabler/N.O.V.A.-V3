"""Docker control.

Uses the Docker SDK when it is installed and the socket is reachable, and falls
back to the ``docker`` CLI otherwise — a lot of home servers have the binary but
not the Python package, and the assistant should work on both.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from ..runtime.errors import IntegrationError
from ..runtime.logging import get_logger
from .metrics import format_bytes

log = get_logger(__name__)


@dataclass(slots=True)
class Container:
    id: str
    name: str
    image: str
    status: str
    state: str
    created: str = ""
    ports: str = ""
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    health: str = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id[:12],
            "name": self.name,
            "image": self.image,
            "status": self.status,
            "state": self.state,
            "health": self.health,
            "ports": self.ports,
            "cpuPercent": round(self.cpu_percent, 1),
            "memoryMb": round(self.memory_mb, 1),
        }

    def describe(self) -> str:
        bits = [f"{self.name} ({self.state}"]
        if self.health:
            bits.append(f", {self.health}")
        bits.append(f") — {self.image}")
        if self.status:
            bits.append(f", {self.status}")
        return "".join(bits)


class DockerManager:
    """Container inspection and lifecycle control."""

    def __init__(self, socket_url: str = "unix:///var/run/docker.sock") -> None:
        self.socket_url = socket_url
        self._client: Any = None
        self._backend = "none"

    async def connect(self) -> str:
        """Return the active backend: ``sdk``, ``cli`` or ``none``."""
        if self._backend != "none":
            return self._backend
        if await asyncio.to_thread(self._try_sdk):
            self._backend = "sdk"
        elif await self._try_cli():
            self._backend = "cli"
        return self._backend

    def _try_sdk(self) -> bool:
        try:
            import docker

            client = docker.DockerClient(base_url=self.socket_url, timeout=10)
            client.ping()
            self._client = client
            return True
        except Exception as exc:  # noqa: BLE001 - no socket, no package, no permission
            log.debug("docker_sdk_unavailable", error=str(exc))
            return False

    async def _try_cli(self) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8)
            return process.returncode == 0 and bool(stdout.strip())
        except (TimeoutError, OSError):
            return False

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def available(self) -> bool:
        return self._backend != "none"

    def _require(self) -> None:
        if not self.available:
            raise IntegrationError(
                "docker", "not reachable — is the daemon running and are you in the docker group?"
            )

    # ------------------------------------------------------------------ query

    async def list_containers(self, *, all_containers: bool = True) -> list[Container]:
        await self.connect()
        self._require()
        if self._backend == "sdk":
            return await asyncio.to_thread(self._list_sdk, all_containers)
        return await self._list_cli(all_containers)

    def _list_sdk(self, all_containers: bool) -> list[Container]:
        out: list[Container] = []
        for raw in self._client.containers.list(all=all_containers):
            attrs = raw.attrs or {}
            state = attrs.get("State", {})
            out.append(
                Container(
                    id=raw.id or "",
                    name=raw.name,
                    image=_first_tag(raw),
                    status=state.get("Status", raw.status),
                    state=state.get("Status", raw.status),
                    created=attrs.get("Created", ""),
                    health=(state.get("Health") or {}).get("Status", ""),
                    ports=_format_ports(attrs.get("NetworkSettings", {}).get("Ports", {})),
                )
            )
        return out

    async def _list_cli(self, all_containers: bool) -> list[Container]:
        args = ["docker", "ps", "--format", "{{json .}}"]
        if all_containers:
            args.append("-a")
        stdout = await self._cli(*args)
        out: list[Container] = []
        for line in stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(
                Container(
                    id=data.get("ID", ""),
                    name=data.get("Names", ""),
                    image=data.get("Image", ""),
                    status=data.get("Status", ""),
                    state=data.get("State", ""),
                    created=data.get("CreatedAt", ""),
                    ports=data.get("Ports", ""),
                )
            )
        return out

    async def find(self, name: str) -> Container:
        containers = await self.list_containers()
        needle = name.strip().lower().lstrip("/")
        for container in containers:
            if container.name.lower().lstrip("/") == needle:
                return container
        # Fall back to a substring match — voice rarely produces exact names.
        matches = [c for c in containers if needle in c.name.lower() or needle in c.image.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise IntegrationError("docker", f"no container matching '{name}'")
        raise IntegrationError(
            "docker", f"'{name}' matches {len(matches)}: {', '.join(c.name for c in matches[:5])}"
        )

    async def stats(self) -> list[Container]:
        """Live CPU/memory for running containers."""
        await self.connect()
        self._require()
        stdout = await self._cli(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}"}',
        )
        out: list[Container] = []
        for line in stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            container = Container(id="", name=data["name"], image="", status="", state="running")
            container.cpu_percent = _percent(data.get("cpu", "0%"))
            container.memory_mb = _memory_mb(data.get("mem", "0B / 0B"))
            out.append(container)
        return out

    async def logs(self, name: str, *, lines: int = 100, since: str = "") -> str:
        await self.connect()
        self._require()
        container = await self.find(name)
        args = ["docker", "logs", "--tail", str(lines), "--timestamps"]
        if since:
            args += ["--since", since]
        args.append(container.name)
        return await self._cli(*args, merge_stderr=True)

    async def inspect(self, name: str) -> dict[str, Any]:
        await self.connect()
        self._require()
        container = await self.find(name)
        stdout = await self._cli("docker", "inspect", container.name)
        try:
            payload = json.loads(stdout)
            return payload[0] if isinstance(payload, list) and payload else {}
        except json.JSONDecodeError:
            return {}

    # ---------------------------------------------------------------- control

    async def start(self, name: str) -> str:
        return await self._lifecycle("start", name)

    async def stop(self, name: str) -> str:
        return await self._lifecycle("stop", name)

    async def restart(self, name: str) -> str:
        return await self._lifecycle("restart", name)

    async def _lifecycle(self, action: str, name: str) -> str:
        await self.connect()
        self._require()
        container = await self.find(name)
        if self._backend == "sdk":
            await asyncio.to_thread(
                lambda: getattr(self._client.containers.get(container.id), action)()
            )
        else:
            await self._cli("docker", action, container.name)
        log.info("container_lifecycle", action=action, container=container.name)
        return f"{action}ed {container.name}"

    async def pull(self, image: str) -> str:
        await self.connect()
        self._require()
        await self._cli("docker", "pull", image, timeout=600)
        return f"pulled {image}"

    async def compose(self, project_dir: str, action: str) -> str:
        """Run ``docker compose <action>`` in a project directory."""
        await self.connect()
        self._require()
        if action not in ("up", "down", "restart", "pull", "ps", "logs"):
            raise IntegrationError("docker", f"unsupported compose action '{action}'")
        args = ["docker", "compose", action]
        if action == "up":
            args += ["-d"]
        stdout = await self._cli(*args, cwd=project_dir, timeout=600, merge_stderr=True)
        return stdout.strip()[-2000:] or f"compose {action} complete"

    async def disk_usage(self) -> str:
        await self.connect()
        self._require()
        return await self._cli("docker", "system", "df")

    # ------------------------------------------------------------------- util

    async def _cli(
        self, *args: str, timeout: float = 30.0, cwd: str | None = None, merge_stderr: bool = False
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except FileNotFoundError as exc:
            raise IntegrationError("docker", "the docker CLI is not installed") from exc
        except TimeoutError as exc:
            raise IntegrationError("docker", f"'{args[1]}' timed out") from exc
        if process.returncode != 0:
            detail = (stderr or stdout or b"").decode("utf-8", "replace").strip()
            raise IntegrationError("docker", detail[:400] or f"exit {process.returncode}")
        return stdout.decode("utf-8", "replace")

    def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # best-effort close
                self._client.close()
            self._client = None


def _first_tag(container: Any) -> str:
    try:
        tags = container.image.tags
        return tags[0] if tags else (container.image.short_id or "")
    except Exception:  # noqa: BLE001
        return ""


def _format_ports(ports: dict[str, Any]) -> str:
    parts: list[str] = []
    for internal, bindings in (ports or {}).items():
        if not bindings:
            continue
        for binding in bindings:
            parts.append(f"{binding.get('HostPort', '')}→{internal}")
    return ", ".join(parts[:6])


def _percent(raw: str) -> float:
    try:
        return float(raw.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _memory_mb(raw: str) -> float:
    """Parse docker's ``123.4MiB / 2GiB`` usage string into megabytes."""
    used = raw.split("/")[0].strip()
    units = {
        "B": 1 / 1024**2,
        "KIB": 1 / 1024,
        "MIB": 1.0,
        "GIB": 1024.0,
        "KB": 1 / 1024,
        "MB": 1.0,
        "GB": 1024.0,
    }
    for suffix, factor in units.items():
        if used.upper().endswith(suffix):
            try:
                return float(used[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    return 0.0


__all__ = ["Container", "DockerManager", "format_bytes"]
