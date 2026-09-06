"""Host telemetry: CPU, memory, disk, network, temperatures, GPU.

Sampling is deliberately cheap — this runs every few seconds forever. Values
that never change (core count, total RAM, hostname) are captured once at
startup; per-tick work is limited to counters psutil can read without walking
``/proc`` in full.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from ..runtime.logging import get_logger
from .gpu import GPUInfo, GPUMonitor

log = get_logger(__name__)

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a base dependency
    psutil = None  # type: ignore[assignment]


@dataclass(slots=True)
class HostInfo:
    hostname: str
    platform: str
    release: str
    architecture: str
    cpu_model: str
    physical_cores: int
    logical_cores: int
    total_memory_gb: float
    boot_time: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "platform": self.platform,
            "release": self.release,
            "architecture": self.architecture,
            "cpuModel": self.cpu_model,
            "physicalCores": self.physical_cores,
            "logicalCores": self.logical_cores,
            "totalMemoryGb": round(self.total_memory_gb, 2),
            "bootTime": self.boot_time,
            "uptimeSeconds": round(time.time() - self.boot_time),
        }


@dataclass(slots=True)
class DiskUsage:
    mountpoint: str
    device: str
    total_gb: float
    used_gb: float
    percent: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "mountpoint": self.mountpoint,
            "device": self.device,
            "totalGb": round(self.total_gb, 1),
            "usedGb": round(self.used_gb, 1),
            "percent": round(self.percent, 1),
        }


@dataclass(slots=True)
class Metrics:
    timestamp: float = field(default_factory=time.time)
    cpu_percent: float = 0.0
    cpu_per_core: list[float] = field(default_factory=list)
    load_average: tuple[float, float, float] = (0.0, 0.0, 0.0)
    memory_percent: float = 0.0
    memory_used_gb: float = 0.0
    memory_total_gb: float = 0.0
    swap_percent: float = 0.0
    disks: list[DiskUsage] = field(default_factory=list)
    gpus: list[GPUInfo] = field(default_factory=list)
    temperatures: dict[str, float] = field(default_factory=dict)
    net_sent_mbps: float = 0.0
    net_recv_mbps: float = 0.0
    process_count: int = 0
    uptime_seconds: float = 0.0

    def as_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cpu": {
                "percent": round(self.cpu_percent, 1),
                "perCore": [round(c, 1) for c in self.cpu_per_core],
                "loadAverage": [round(v, 2) for v in self.load_average],
            },
            "memory": {
                "percent": round(self.memory_percent, 1),
                "usedGb": round(self.memory_used_gb, 2),
                "totalGb": round(self.memory_total_gb, 2),
                "swapPercent": round(self.swap_percent, 1),
            },
            "disks": [d.as_payload() for d in self.disks],
            "gpus": [g.as_payload() for g in self.gpus],
            "temperatures": {k: round(v, 1) for k, v in self.temperatures.items()},
            "network": {
                "sentMbps": round(self.net_sent_mbps, 2),
                "recvMbps": round(self.net_recv_mbps, 2),
            },
            "processes": self.process_count,
            "uptimeSeconds": round(self.uptime_seconds),
        }

    def summary(self) -> str:
        """One-line natural-language summary the model can read directly."""
        parts = [
            f"CPU {self.cpu_percent:.0f}%",
            f"RAM {self.memory_percent:.0f}% "
            f"({self.memory_used_gb:.1f}/{self.memory_total_gb:.1f} GB)",
        ]
        for disk in self.disks[:3]:
            parts.append(f"disk {disk.mountpoint} {disk.percent:.0f}%")
        for gpu in self.gpus:
            gpu_part = f"GPU{gpu.index} {gpu.utilisation:.0f}%"
            if gpu.temperature_c:
                gpu_part += f" @ {gpu.temperature_c:.0f}°C"
            parts.append(gpu_part)
        if self.temperatures:
            hottest = max(self.temperatures.items(), key=lambda kv: kv[1])
            parts.append(f"{hottest[0]} {hottest[1]:.0f}°C")
        parts.append(f"up {format_duration(self.uptime_seconds)}")
        return ", ".join(parts)


class MetricsCollector:
    """Samples the host. One instance, polled on an interval."""

    def __init__(self) -> None:
        self.gpu = GPUMonitor()
        self._last_net: tuple[float, float, float] | None = None
        self._host: HostInfo | None = None

    @property
    def available(self) -> bool:
        return psutil is not None

    async def host_info(self) -> HostInfo:
        if self._host is None:
            self._host = await asyncio.to_thread(self._collect_host)
        return self._host

    def _collect_host(self) -> HostInfo:
        cpu_model = platform.processor() or platform.machine()
        # platform.processor() is empty on most Linux builds; /proc has the real name.
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        cpu_model = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
        total_memory = psutil.virtual_memory().total if psutil else 0
        return HostInfo(
            hostname=socket.gethostname(),
            platform=platform.system(),
            release=platform.release(),
            architecture=platform.machine(),
            cpu_model=cpu_model,
            physical_cores=(psutil.cpu_count(logical=False) or 0) if psutil else 0,
            logical_cores=(psutil.cpu_count(logical=True) or 0) if psutil else 0,
            total_memory_gb=total_memory / 1024**3,
            boot_time=psutil.boot_time() if psutil else time.time(),
        )

    async def sample(self) -> Metrics:
        if psutil is None:
            return Metrics()
        metrics = await asyncio.to_thread(self._sample_sync)
        metrics.gpus = await self.gpu.sample()
        return metrics

    def _sample_sync(self) -> Metrics:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        metrics = Metrics(
            # interval=None returns the delta since the previous call — non-blocking,
            # which matters because this runs on the sampling cadence, not a sleep.
            cpu_percent=psutil.cpu_percent(interval=None),
            cpu_per_core=psutil.cpu_percent(interval=None, percpu=True),
            memory_percent=virtual.percent,
            memory_used_gb=(virtual.total - virtual.available) / 1024**3,
            memory_total_gb=virtual.total / 1024**3,
            swap_percent=swap.percent,
            uptime_seconds=time.time() - psutil.boot_time(),
        )
        if hasattr(psutil, "getloadavg"):
            with contextlib.suppress(OSError, AttributeError):
                metrics.load_average = psutil.getloadavg()
        metrics.disks = self._disks()
        metrics.temperatures = self._temperatures()
        metrics.net_sent_mbps, metrics.net_recv_mbps = self._network()
        with contextlib.suppress(OSError):
            metrics.process_count = len(psutil.pids())
        return metrics

    def _disks(self) -> list[DiskUsage]:
        out: list[DiskUsage] = []
        seen: set[str] = set()
        for partition in psutil.disk_partitions(all=False):
            # Skip pseudo/loop mounts — snap packages alone add dozens.
            if partition.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay", ""):
                continue
            if partition.device in seen:
                continue
            seen.add(partition.device)
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except (PermissionError, OSError):
                continue
            out.append(
                DiskUsage(
                    mountpoint=partition.mountpoint,
                    device=partition.device,
                    total_gb=usage.total / 1024**3,
                    used_gb=usage.used / 1024**3,
                    percent=usage.percent,
                )
            )
        out.sort(key=lambda d: d.total_gb, reverse=True)
        return out[:8]

    def _temperatures(self) -> dict[str, float]:
        if not hasattr(psutil, "sensors_temperatures"):
            return {}
        try:
            readings = psutil.sensors_temperatures()
        except (OSError, AttributeError):
            return {}
        out: dict[str, float] = {}
        for chip, entries in readings.items():
            for entry in entries:
                if entry.current is None or entry.current <= 0:
                    continue
                label = entry.label or chip
                # Keep the hottest reading per label — boards report many cores.
                out[label] = max(out.get(label, 0.0), float(entry.current))
        return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True)[:8])

    def _network(self) -> tuple[float, float]:
        try:
            counters = psutil.net_io_counters()
        except (OSError, AttributeError):
            return 0.0, 0.0
        now = time.monotonic()
        current = (now, float(counters.bytes_sent), float(counters.bytes_recv))
        if self._last_net is None:
            self._last_net = current
            return 0.0, 0.0
        elapsed = current[0] - self._last_net[0]
        if elapsed <= 0:
            return 0.0, 0.0
        sent = (current[1] - self._last_net[1]) * 8 / elapsed / 1_000_000
        recv = (current[2] - self._last_net[2]) * 8 / elapsed / 1_000_000
        self._last_net = current
        return max(0.0, sent), max(0.0, recv)

    def close(self) -> None:
        self.gpu.close()


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"
