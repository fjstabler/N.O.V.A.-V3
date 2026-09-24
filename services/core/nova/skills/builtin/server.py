"""Server administration: telemetry, containers, units, logs, files, commands.

This is the skill that makes N.O.V.A. feel like an administrator rather than a
search box. Read-only tools run freely; anything that restarts a service, edits
a file or reboots the machine is marked destructive and routed through the
confirmation gate before it executes.
"""

from __future__ import annotations

from typing import Annotated, Literal

from ...runtime import Topics
from ...runtime.errors import PermissionDenied, SkillError
from ...system.metrics import format_bytes, format_duration
from ...system.service import SystemService
from ...system.shell import Risk
from ..base import Param, Skill, tool


class ServerSkill(Skill):
    name = "server"
    description = "Monitor and administer the Ubuntu server: metrics, Docker, systemd, logs, files."
    category = "Server"

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.server.enabled:
            return False, "server integration disabled in settings"
        if self.ctx.service("system") is None:
            return False, "system service is not running"
        return True, ""

    @property
    def system(self) -> SystemService:
        return self.ctx.require("system", SystemService)

    def context_lines(self) -> list[str]:
        system = self.ctx.service("system", SystemService)
        if system is None or system.host is None:
            return []
        lines = [f"Server: {system.host.hostname}, {system.host.platform} {system.host.release}."]
        if system.latest is not None:
            lines.append(f"Right now: {system.latest.summary()}.")
        return lines

    # ------------------------------------------------------------- telemetry

    @tool("Get current server metrics: CPU, memory, disk, GPU, temperatures and uptime.")
    async def status(self) -> str:
        metrics = await self.system.current_metrics()
        host = self.system.host
        header = (
            f"{host.hostname} ({host.cpu_model}, {host.logical_cores} cores)" if host else "server"
        )
        return f"{header}. {metrics.summary()}."

    @tool(
        "Show CPU temperature, RAM usage and disk usage as three live rings on screen. "
        "Use for 'how's my system', 'what are the system stats', 'CPU temperature', 'RAM usage', "
        "'how much storage is left', 'show the system'. The spoken reply is short — the picture "
        "on screen carries the numbers."
    )
    async def system_rings(self) -> str:
        metrics = await self.system.current_metrics()
        rings = _build_system_rings(metrics)
        self.ctx.bus.publish(
            Topics.UI_SURFACE_SHOW,
            {"kind": "system-stats", "title": "System", "rings": rings},
            source=self.name,
        )
        # Spoken form: a single readable sentence, the surface carries the detail.
        return ", ".join(f"{r['label']} {r['value']}" for r in rings) + "."

    @tool("Get detailed disk usage for every mounted filesystem.")
    async def disk_usage(self) -> str:
        metrics = await self.system.current_metrics()
        if not metrics.disks:
            return "No filesystems reported."
        return "\n".join(
            f"{d.mountpoint}: {d.percent:.0f}% used, "
            f"{d.total_gb - d.used_gb:.0f} GB free of {d.total_gb:.0f} GB"
            for d in metrics.disks
        )

    @tool("Get GPU utilisation, memory, temperature and power draw.")
    async def gpu_status(self) -> str:
        metrics = await self.system.current_metrics()
        if not metrics.gpus:
            return "No GPU detected on this machine."
        return "\n".join(
            f"{g.name}: {g.utilisation:.0f}% load, "
            f"{g.memory_used_mb:.0f}/{g.memory_total_mb:.0f} MB, "
            f"{g.temperature_c:.0f}°C, {g.power_watts:.0f} W"
            for g in metrics.gpus
        )

    @tool("List the processes using the most CPU or memory.")
    async def top_processes(
        self,
        by: Annotated[Literal["cpu", "memory"], Param("Sort key")] = "cpu",
        limit: Annotated[int, Param("How many to return")] = 8,
    ) -> str:
        import psutil

        processes = []
        for proc in psutil.process_iter(["name", "cpu_percent", "memory_info", "username"]):
            try:
                info = proc.info
                processes.append(
                    (
                        info["name"] or "?",
                        float(info["cpu_percent"] or 0.0),
                        float(info["memory_info"].rss if info["memory_info"] else 0),
                        info["username"] or "",
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        index = 1 if by == "cpu" else 2
        processes.sort(key=lambda p: p[index], reverse=True)
        return "\n".join(
            f"{name}: {cpu:.1f}% CPU, {format_bytes(rss)} RAM ({user})"
            for name, cpu, rss, user in processes[: max(1, min(limit, 20))]
        )

    @tool("Get how long the server has been running.")
    async def uptime(self) -> str:
        metrics = await self.system.current_metrics()
        return f"Up {format_duration(metrics.uptime_seconds)}."

    # -------------------------------------------------------------- containers

    @tool("List Docker containers and their state.")
    async def list_containers(
        self,
        running_only: Annotated[bool, Param("Only show running containers")] = False,
    ) -> str:
        containers = await self.system.docker.list_containers(all_containers=not running_only)
        if not containers:
            return "No containers found."
        return "\n".join(c.describe() for c in containers)

    @tool("Get the recent log output of a Docker container.")
    async def container_logs(
        self,
        name: Annotated[str, Param("Container name", examples=("jellyfin", "immich_server"))],
        lines: Annotated[int, Param("How many lines from the end")] = 60,
        since: Annotated[str, Param("Optional window, e.g. '10m' or '2h'")] = "",
    ) -> str:
        logs = await self.system.docker.logs(name, lines=max(1, min(lines, 500)), since=since)
        return logs[-6000:] or "(no output)"

    @tool("Restart a Docker container.", destructive=True)
    async def restart_container(self, name: Annotated[str, Param("Container name")]) -> str:
        return await self.system.docker.restart(name)

    @tool("Start a stopped Docker container.", mutating=True)
    async def start_container(self, name: Annotated[str, Param("Container name")]) -> str:
        return await self.system.docker.start(name)

    @tool("Stop a running Docker container.", destructive=True)
    async def stop_container(self, name: Annotated[str, Param("Container name")]) -> str:
        return await self.system.docker.stop(name)

    @tool("Get live CPU and memory usage per container.")
    async def container_stats(self) -> str:
        stats = await self.system.docker.stats()
        if not stats:
            return "No running containers."
        return "\n".join(f"{c.name}: {c.cpu_percent:.1f}% CPU, {c.memory_mb:.0f} MB" for c in stats)

    @tool("Run a docker compose action in a project directory.", destructive=True)
    async def compose(
        self,
        project_dir: Annotated[str, Param("Directory holding docker-compose.yml")],
        action: Annotated[Literal["up", "down", "restart", "pull", "ps"], Param("Action")],
    ) -> str:
        # Reuse the file sandbox so compose cannot be pointed outside the roots.
        path = self.system.files.resolve(project_dir)
        return await self.system.docker.compose(str(path), action)

    # ------------------------------------------------------------------ units

    @tool("Check the status of a systemd service.")
    async def service_status(
        self, unit: Annotated[str, Param("Unit name", examples=("nginx", "docker.service"))]
    ) -> str:
        return (await self.system.systemd.status(unit)).describe()

    @tool("List systemd services that have failed.")
    async def failed_services(self) -> str:
        units = await self.system.systemd.failed_units()
        if not units:
            return "No failed units."
        return "\n".join(u.describe() for u in units)

    @tool("Start, stop, restart or reload a systemd service.", destructive=True)
    async def control_service(
        self,
        action: Annotated[Literal["start", "stop", "restart", "reload"], Param("What to do")],
        unit: Annotated[str, Param("Unit name")],
    ) -> str:
        return await self.system.systemd.control(action, unit)

    @tool("Search the system journal, optionally filtered by unit or text.")
    async def search_logs(
        self,
        unit: Annotated[str, Param("Unit to filter by; empty for all")] = "",
        contains: Annotated[str, Param("Only lines containing this text")] = "",
        lines: Annotated[int, Param("How many lines")] = 80,
        since: Annotated[str, Param("Time window, e.g. '1 hour ago', 'today'")] = "",
        errors_only: Annotated[bool, Param("Only errors and worse")] = False,
    ) -> str:
        output = await self.system.systemd.journal(
            unit,
            lines=lines,
            since=since,
            grep=contains,
            priority="err" if errors_only else "",
        )
        return output[-6000:] or "(nothing matched)"

    @tool("Check for pending system package updates.")
    async def check_updates(self) -> str:
        info = await self.system.systemd.pending_updates()
        if not info.get("available"):
            return "Package management is not available on this host."
        count, security = info["count"], info["security"]
        if count == 0:
            return "The system is up to date."
        text = f"{count} update{'s' if count != 1 else ''} available"
        if security:
            text += f", {security} of them security updates"
        if info.get("rebootRequired"):
            text += ". A reboot is required."
        return text + "."

    @tool("Reboot the server.", destructive=True)
    async def reboot(self) -> str:
        result = await self.system.runner.run("systemctl reboot")
        return "Reboot initiated." if result.ok else f"Reboot failed: {result.stderr[:200]}"

    # ------------------------------------------------------------------ files

    @tool("List files in a directory.")
    async def list_files(
        self,
        path: Annotated[str, Param("Directory path")],
        pattern: Annotated[str, Param("Optional glob, e.g. '*.log'")] = "",
    ) -> str:
        entries = await self.system.files.list_dir(path, pattern=pattern)
        if not entries:
            return "(empty)"
        return "\n".join(
            f"{'dir ' if e.is_dir else 'file'} {e.name}"
            + ("" if e.is_dir else f" ({format_bytes(e.size)})")
            for e in entries
        )

    @tool("Read the contents of a text file.")
    async def read_file(self, path: Annotated[str, Param("File path")]) -> str:
        return await self.system.files.read(path)

    @tool("Write text to a file, replacing its contents.", destructive=True)
    async def write_file(
        self,
        path: Annotated[str, Param("File path")],
        content: Annotated[str, Param("Full contents to write")],
    ) -> str:
        return await self.system.files.write(path, content)

    @tool("Append text to the end of a file.", mutating=True)
    async def append_file(
        self,
        path: Annotated[str, Param("File path")],
        content: Annotated[str, Param("Text to append")],
    ) -> str:
        return await self.system.files.write(path, content, append=True)

    @tool("Create a directory.", mutating=True)
    async def create_directory(self, path: Annotated[str, Param("Directory path")]) -> str:
        return await self.system.files.make_dir(path)

    @tool("Delete a file or directory.", destructive=True)
    async def delete_path(
        self,
        path: Annotated[str, Param("Path to delete")],
        recursive: Annotated[bool, Param("Required to delete a directory")] = False,
    ) -> str:
        return await self.system.files.delete(path, recursive=recursive)

    @tool("Find files by name beneath a directory.")
    async def find_files(
        self,
        root: Annotated[str, Param("Directory to search from")],
        pattern: Annotated[str, Param("Glob pattern, e.g. '*.conf'")],
    ) -> str:
        entries = await self.system.files.search(root, pattern)
        if not entries:
            return "No matches."
        return "\n".join(f"{e.path} ({format_bytes(e.size)})" for e in entries[:60])

    @tool("Search for text inside files beneath a directory.")
    async def grep_files(
        self,
        root: Annotated[str, Param("Directory to search from")],
        text: Annotated[str, Param("Text to look for")],
        file_glob: Annotated[str, Param("Restrict to matching filenames")] = "*",
    ) -> str:
        matches = await self.system.files.grep(root, text, glob=file_glob)
        return "\n".join(matches) if matches else "No matches."

    @tool("Find what is using the most disk space under a path.")
    async def largest_items(
        self,
        path: Annotated[str, Param("Directory to analyse")],
    ) -> str:
        items = await self.system.files.disk_tree(path)
        if not items:
            return "(nothing found)"
        return "\n".join(f"{name}: {format_bytes(size)}" for name, size in items)

    # -------------------------------------------------------------- execution

    @tool(
        "Run a shell command on the server. Inspection commands run immediately; "
        "anything that changes state is confirmed with the user first.",
        destructive=True,
    )
    async def run_command(
        self,
        command: Annotated[str, Param("The exact command line to run")],
    ) -> str:
        verdict = self.system.runner.classify(command)
        if verdict.risk is Risk.FORBIDDEN:
            raise PermissionDenied(f"I won't run that: {verdict.reason}.")
        result = await self.system.runner.run(command)
        return result.summarise()

    @tool("Run a read-only inspection command. Refuses anything that changes state.")
    async def inspect(
        self,
        command: Annotated[str, Param("A read-only command, e.g. 'df -h' or 'docker ps'")],
    ) -> str:
        verdict = self.system.runner.classify(command)
        if verdict.risk is not Risk.READ_ONLY:
            raise SkillError(
                f"'{command}' is not read-only ({verdict.reason}) — use run_command for that."
            )
        result = await self.system.runner.run(command)
        return result.summarise()


def _build_system_rings(metrics: object) -> list[dict[str, object]]:
    """Turn a Metrics snapshot into the three rings the system-stats surface shows.

    Each ring carries a formatted centre value, a 0..1 fraction for the arc, and a
    tone (warn/critical) once it crosses conservative thresholds so the frontend
    can colour it. Kept pure so it is testable without a psutil sample.
    """
    m = metrics  # a nova.system.metrics.Metrics, but duck-typed for testability

    # CPU temp — take the hottest sensor. Falls back to CPU % if none, so the
    # ring is never empty (some machines / VMs report no temperature at all).
    temps = getattr(m, "temperatures", {}) or {}
    if temps:
        temp = max(temps.values())
        cpu_ring = {
            "label": "CPU temp",
            "value": f"{round(temp)}°C",
            # 30° → empty, 90° → full; typical laptops idle ~40, throttle ~95.
            "fraction": max(0.0, min(1.0, (temp - 30.0) / 60.0)),
            "caption": _hottest_label(temps),
            "tone": "critical" if temp >= 85 else "warn" if temp >= 75 else "ok",
        }
    else:
        cpu_pct = float(getattr(m, "cpu_percent", 0.0))
        cpu_ring = {
            "label": "CPU load",
            "value": f"{round(cpu_pct)}%",
            "fraction": max(0.0, min(1.0, cpu_pct / 100.0)),
            "caption": "no temperature sensor",
            "tone": "critical" if cpu_pct >= 90 else "warn" if cpu_pct >= 75 else "ok",
        }

    # RAM %.
    ram_pct = float(getattr(m, "memory_percent", 0.0))
    used = float(getattr(m, "memory_used_gb", 0.0))
    total = float(getattr(m, "memory_total_gb", 0.0))
    ram_ring = {
        "label": "RAM",
        "value": f"{round(ram_pct)}%",
        "fraction": max(0.0, min(1.0, ram_pct / 100.0)),
        "caption": f"{used:.1f} of {total:.0f} GB" if total > 0 else "",
        "tone": "critical" if ram_pct >= 92 else "warn" if ram_pct >= 80 else "ok",
    }

    # Disk — the largest mount; usually / on a desktop machine.
    disks = getattr(m, "disks", []) or []
    if disks:
        disk = max(disks, key=lambda d: getattr(d, "total_gb", 0.0))
        free_gb = float(getattr(disk, "total_gb", 0.0)) - float(getattr(disk, "used_gb", 0.0))
        used_pct = float(getattr(disk, "percent", 0.0))
        free_pct = max(0.0, 100.0 - used_pct)
        disk_ring = {
            "label": "Disk free",
            # Free is the useful number for "storage remaining", the ring shows
            # what's used so a nearly-full disk looks visibly full.
            "value": f"{round(free_pct)}%",
            "fraction": max(0.0, min(1.0, used_pct / 100.0)),
            "caption": f"{free_gb:.0f} GB free of {getattr(disk, 'total_gb', 0.0):.0f}",
            "tone": "critical" if used_pct >= 92 else "warn" if used_pct >= 85 else "ok",
        }
    else:
        disk_ring = {
            "label": "Disk free",
            "value": "—",
            "fraction": 0.0,
            "caption": "no disk reported",
            "tone": "ok",
        }

    return [cpu_ring, ram_ring, disk_ring]


def _hottest_label(temps: dict[str, float]) -> str:
    hottest = max(temps.items(), key=lambda kv: kv[1])
    return hottest[0]
