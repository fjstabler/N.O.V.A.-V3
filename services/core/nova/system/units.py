"""systemd units and the journal.

Everything goes through ``systemctl``/``journalctl`` rather than the D-Bus API:
no extra dependency, identical behaviour to what the user would type, and the
output is what they would see if they checked by hand.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.errors import IntegrationError, PermissionDenied
from ..runtime.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Unit:
    name: str
    load: str
    active: str
    sub: str
    description: str

    @property
    def healthy(self) -> bool:
        return self.active == "active"

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "load": self.load,
            "active": self.active,
            "sub": self.sub,
            "description": self.description,
            "healthy": self.healthy,
        }

    def describe(self) -> str:
        return f"{self.name}: {self.active}/{self.sub} — {self.description}"


class SystemdManager:
    """Query and control systemd units."""

    def __init__(self, managed_units: tuple[str, ...] = ()) -> None:
        #: Units the operator has pre-approved for lifecycle actions.
        self.managed_units = tuple(managed_units)
        self._available: bool | None = None

    async def probe(self) -> bool:
        if self._available is None:
            self._available = (
                shutil.which("systemctl") is not None and await self._systemd_running()
            )
        return self._available

    async def _systemd_running(self) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "is-system-running",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except (TimeoutError, OSError):
            return False
        # "degraded", "starting" and "maintenance" all still mean systemd is there.
        return stdout.decode().strip() not in ("offline", "")

    async def list_units(self, pattern: str = "", *, failed_only: bool = False) -> list[Unit]:
        args = ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--output=json"]
        if failed_only:
            args.append("--state=failed")
        if pattern:
            args.append(f"{pattern}*")
        stdout = await self._run(*args)
        try:
            raw = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return []
        return [
            Unit(
                name=item.get("unit", ""),
                load=item.get("load", ""),
                active=item.get("active", ""),
                sub=item.get("sub", ""),
                description=item.get("description", ""),
            )
            for item in raw
        ]

    async def status(self, unit: str) -> Unit:
        name = _normalise(unit)
        stdout = await self._run(
            "systemctl",
            "show",
            name,
            "--property=Id,LoadState,ActiveState,SubState,Description",
            "--no-pager",
        )
        fields: dict[str, str] = {}
        for line in stdout.splitlines():
            key, _, value = line.partition("=")
            fields[key] = value
        if not fields.get("Id"):
            raise IntegrationError("systemd", f"no unit named '{unit}'")
        return Unit(
            name=fields.get("Id", name),
            load=fields.get("LoadState", ""),
            active=fields.get("ActiveState", ""),
            sub=fields.get("SubState", ""),
            description=fields.get("Description", ""),
        )

    async def control(self, action: str, unit: str) -> str:
        if action not in ("start", "stop", "restart", "reload"):
            raise IntegrationError("systemd", f"unsupported action '{action}'")
        name = _normalise(unit)
        # Empty means unrestricted (matches DesktopSettings.app_allowlist's own
        # "empty = any" convention) — but a non-empty list is the operator's
        # explicit scoping decision, and it was previously stored but never
        # checked here, so it silently did nothing.
        if self.managed_units and name not in self.managed_units:
            raise PermissionDenied(
                f"'{name}' is not in the allowed unit list (server.managed_units) — "
                "add it there first if this should be controllable"
            )
        await self._run("systemctl", action, name, check=True)
        log.info("unit_control", action=action, unit=name)
        after = await self.status(name)
        return f"{action}ed {name} — now {after.active}/{after.sub}"

    async def failed_units(self) -> list[Unit]:
        return [u for u in await self.list_units(failed_only=True) if u.active == "failed"]

    async def journal(
        self,
        unit: str = "",
        *,
        lines: int = 100,
        since: str = "",
        grep: str = "",
        priority: str = "",
    ) -> str:
        args = ["journalctl", "--no-pager", "-n", str(max(1, min(lines, 2000)))]
        if unit:
            args += ["-u", _normalise(unit)]
        if since:
            args += ["--since", since]
        if grep:
            args += ["--grep", grep]
        if priority:
            args += ["-p", priority]
        return await self._run(*args, timeout=30)

    async def pending_updates(self) -> dict[str, Any]:
        """Count upgradable APT packages, flagging security updates separately."""
        if shutil.which("apt-get") is None:
            return {"available": False}
        stdout = await self._run("apt-get", "-s", "upgrade", timeout=60)
        upgrades = [line for line in stdout.splitlines() if line.startswith("Inst ")]
        security = [line for line in upgrades if "security" in line.lower()]
        reboot_required = await asyncio.to_thread(_reboot_required)
        return {
            "available": True,
            "count": len(upgrades),
            "security": len(security),
            "rebootRequired": reboot_required,
            "packages": [line.split()[1] for line in upgrades[:25] if len(line.split()) > 1],
        }

    async def _run(self, *args: str, timeout: float = 15.0, check: bool = False) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0", "LC_ALL": "C"},
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except FileNotFoundError as exc:
            raise IntegrationError("systemd", f"{args[0]} is not installed") from exc
        except TimeoutError as exc:
            raise IntegrationError("systemd", f"{args[0]} timed out") from exc
        if check and process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            # The common failure here is polkit refusing an unprivileged action.
            if "interactive authentication required" in detail.lower():
                raise IntegrationError(
                    "systemd",
                    "permission denied — grant a polkit rule or add a sudoers entry for this unit",
                )
            raise IntegrationError("systemd", detail[:400] or f"exit {process.returncode}")
        return stdout.decode("utf-8", "replace")


def _reboot_required() -> bool:
    """APT drops this file when a package needs a restart to take effect."""
    try:
        return Path("/var/run/reboot-required").exists()
    except OSError:
        return False


def _normalise(unit: str) -> str:
    """Accept 'nginx' as well as 'nginx.service'."""
    unit = unit.strip()
    if not unit:
        raise IntegrationError("systemd", "no unit name given")
    if "." not in unit:
        return f"{unit}.service"
    return unit
