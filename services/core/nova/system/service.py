"""The system service: telemetry loop, threshold alerts and admin handles.

Owns the long-lived objects (metrics collector, Docker client, systemd handle,
file sandbox, command runner) so skills stay stateless and cheap to construct.

Alerts are edge-triggered with hysteresis: an alarm fires once when a threshold
is crossed and re-arms only after the value falls back below a lower mark. A
GPU sitting at 84 °C should notify once, not sixty times a minute.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ..context import NovaContext
from ..runtime import Service, Topics
from .containers import DockerManager
from .files import FileSandbox
from .metrics import HostInfo, Metrics, MetricsCollector
from .shell import CommandRunner
from .units import SystemdManager

#: How far a value must fall below the threshold before the alert re-arms.
HYSTERESIS = 8.0
#: Never repeat the same alert inside this window, even across re-arms.
ALERT_COOLDOWN = 300.0


@dataclass(slots=True)
class _AlertState:
    active: bool = False
    last_fired: float = 0.0


class SystemService(Service):
    """Host monitoring and administration."""

    name = "system"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.metrics = MetricsCollector()
        self.docker = DockerManager(ctx.settings.server.docker_socket)
        self.systemd = SystemdManager(tuple(ctx.settings.server.managed_units))
        self.files = FileSandbox(tuple(ctx.settings.server.file_roots))
        self.runner = CommandRunner(
            timeout=ctx.settings.server.shell_timeout_seconds,
            allow_shell=ctx.settings.server.allow_shell,
            extra_readonly=tuple(ctx.settings.server.extra_readonly_commands),
        )
        self.latest: Metrics | None = None
        self.host: HostInfo | None = None
        self._alerts: dict[str, _AlertState] = {}
        self._backends: dict[str, str] = {}

    async def on_start(self) -> None:
        settings = self.ctx.settings.server
        self.host = await self.metrics.host_info()
        self.latest = await self.metrics.sample()

        self._backends["gpu"] = await self.metrics.gpu.probe()
        if settings.docker_enabled:
            self._backends["docker"] = await self.docker.connect()
        if settings.systemd_enabled:
            self._backends["systemd"] = "yes" if await self.systemd.probe() else "no"

        self.spawn(self._monitor_loop(), name="system-monitor")
        self.ctx.store.on_change(self._on_settings_changed)
        self.log.info("system_ready", host=self.host.hostname, **self._backends)

    async def on_stop(self) -> None:
        self.metrics.close()
        self.docker.close()

    def describe(self) -> str:
        bits = [self.host.hostname if self.host else "?"]
        if self._backends.get("gpu", "none") != "none":
            bits.append(f"gpu:{self._backends['gpu']}")
        if self._backends.get("docker", "none") != "none":
            bits.append(f"docker:{self._backends['docker']}")
        return " · ".join(bits)

    def _on_settings_changed(self, settings: Any, changed: dict[str, Any]) -> None:
        """Re-derive anything that was built from configuration."""
        if any(k.startswith("server.file_roots") for k in changed):
            self.files = FileSandbox(tuple(settings.server.file_roots))
        if any(k.startswith("server.") for k in changed):
            self.runner = CommandRunner(
                timeout=settings.server.shell_timeout_seconds,
                allow_shell=settings.server.allow_shell,
                extra_readonly=tuple(settings.server.extra_readonly_commands),
            )
            self.systemd.managed_units = tuple(settings.server.managed_units)

    # ------------------------------------------------------------- monitoring

    async def _monitor_loop(self) -> None:
        while True:
            interval = self.ctx.settings.server.monitor_interval_seconds
            try:
                await asyncio.sleep(interval)
                self.latest = await self.metrics.sample()
                self.bus.publish(Topics.METRICS, self.latest.as_payload(), source=self.name)
                self._check_thresholds(self.latest)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("metrics_sample_failed")
                await asyncio.sleep(interval)

    def _check_thresholds(self, metrics: Metrics) -> None:
        limits = self.ctx.settings.server
        self._evaluate(
            "cpu",
            metrics.cpu_percent,
            limits.alert_cpu_percent,
            "CPU under sustained load",
            f"{metrics.cpu_percent:.0f}% utilisation",
        )
        self._evaluate(
            "memory",
            metrics.memory_percent,
            limits.alert_memory_percent,
            "Memory pressure",
            f"{metrics.memory_percent:.0f}% used "
            f"({metrics.memory_used_gb:.1f}/{metrics.memory_total_gb:.1f} GB)",
        )
        for disk in metrics.disks:
            self._evaluate(
                f"disk:{disk.mountpoint}",
                disk.percent,
                limits.alert_disk_percent,
                "Disk nearly full",
                f"{disk.mountpoint} at {disk.percent:.0f}% "
                f"({disk.total_gb - disk.used_gb:.0f} GB free)",
            )
        for gpu in metrics.gpus:
            self._evaluate(
                f"gpu:{gpu.index}",
                gpu.temperature_c,
                limits.alert_gpu_temp_c,
                "GPU running hot",
                f"{gpu.name} at {gpu.temperature_c:.0f}°C, {gpu.utilisation:.0f}% load",
            )

    def _evaluate(self, key: str, value: float, threshold: float, title: str, body: str) -> None:
        state = self._alerts.setdefault(key, _AlertState())
        now = time.time()
        if value >= threshold:
            if state.active or now - state.last_fired < ALERT_COOLDOWN:
                return
            state.active = True
            state.last_fired = now
            self.bus.publish(
                Topics.NOTIFICATION,
                {
                    "level": "warning",
                    "title": title,
                    "body": body,
                    "icon": "alert",
                    "source": "system",
                    "timeout": 12.0,
                },
                source=self.name,
            )
            self.log.warning(
                "threshold_exceeded", key=key, value=round(value, 1), threshold=threshold
            )
        elif state.active and value < threshold - HYSTERESIS:
            state.active = False
            self.log.info("threshold_recovered", key=key, value=round(value, 1))

    # -------------------------------------------------------------- accessors

    async def snapshot(self) -> dict[str, Any]:
        """Everything the UI or a tool needs in one call."""
        metrics = self.latest or await self.metrics.sample()
        return {
            "host": self.host.as_payload() if self.host else {},
            "metrics": metrics.as_payload(),
            "backends": dict(self._backends),
        }

    async def current_metrics(self) -> Metrics:
        if self.latest is None:
            self.latest = await self.metrics.sample()
        return self.latest
