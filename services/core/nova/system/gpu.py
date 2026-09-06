"""GPU telemetry.

Tries, in order: NVML (accurate, cheap, needs ``pynvml``), ``nvidia-smi`` (works
on any driver install), then the AMD sysfs hwmon tree. Anything unavailable is
simply absent from the result — the caller renders what it gets.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class GPUInfo:
    index: int
    name: str
    utilisation: float = 0.0  # percent
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    temperature_c: float = 0.0
    power_watts: float = 0.0
    fan_percent: float = 0.0
    vendor: str = "unknown"

    @property
    def memory_percent(self) -> float:
        if self.memory_total_mb <= 0:
            return 0.0
        return self.memory_used_mb / self.memory_total_mb * 100.0

    def as_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "vendor": self.vendor,
            "utilisation": round(self.utilisation, 1),
            "memoryUsedMb": round(self.memory_used_mb),
            "memoryTotalMb": round(self.memory_total_mb),
            "memoryPercent": round(self.memory_percent, 1),
            "temperatureC": round(self.temperature_c, 1),
            "powerWatts": round(self.power_watts, 1),
            "fanPercent": round(self.fan_percent, 1),
        }


class GPUMonitor:
    """Vendor-agnostic GPU sampling with a stable output shape."""

    def __init__(self) -> None:
        self._nvml: Any = None
        self._backend: str = "none"
        self._probed = False

    async def probe(self) -> str:
        if self._probed:
            return self._backend
        self._probed = True
        if await asyncio.to_thread(self._init_nvml):
            self._backend = "nvml"
        elif shutil.which("nvidia-smi"):
            self._backend = "nvidia-smi"
        elif _amd_cards():
            self._backend = "amdgpu"
        log.info("gpu_backend", backend=self._backend)
        return self._backend

    def _init_nvml(self) -> bool:
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            return True
        except Exception:  # noqa: BLE001 - no NVIDIA driver, or no pynvml
            return False

    @property
    def backend(self) -> str:
        return self._backend

    async def sample(self) -> list[GPUInfo]:
        backend = await self.probe()
        if backend == "nvml":
            return await asyncio.to_thread(self._sample_nvml)
        if backend == "nvidia-smi":
            return await self._sample_smi()
        if backend == "amdgpu":
            return await asyncio.to_thread(_sample_amd)
        return []

    def _sample_nvml(self) -> list[GPUInfo]:
        nvml = self._nvml
        out: list[GPUInfo] = []
        try:
            count = nvml.nvmlDeviceGetCount()
        except Exception:  # noqa: BLE001
            return out
        for index in range(count):
            try:
                handle = nvml.nvmlDeviceGetHandleByIndex(index)
                name = nvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode()
                memory = nvml.nvmlDeviceGetMemoryInfo(handle)
                rates = nvml.nvmlDeviceGetUtilizationRates(handle)
                info = GPUInfo(
                    index=index,
                    name=name,
                    vendor="nvidia",
                    utilisation=float(rates.gpu),
                    memory_used_mb=memory.used / 1024 / 1024,
                    memory_total_mb=memory.total / 1024 / 1024,
                )
                info.temperature_c = _safe(
                    lambda h=handle: float(
                        nvml.nvmlDeviceGetTemperature(h, nvml.NVML_TEMPERATURE_GPU)
                    )
                )
                info.power_watts = _safe(lambda h=handle: nvml.nvmlDeviceGetPowerUsage(h) / 1000.0)
                info.fan_percent = _safe(lambda h=handle: float(nvml.nvmlDeviceGetFanSpeed(h)))
                out.append(info)
            except Exception:  # noqa: BLE001 - one bad card must not hide the rest
                continue
        return out

    async def _sample_smi(self) -> list[GPUInfo]:
        query = (
            "index,name,utilization.gpu,memory.used,memory.total,"
            "temperature.gpu,power.draw,fan.speed"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except (TimeoutError, OSError):
            return []

        out: list[GPUInfo] = []
        for line in stdout.decode().strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            out.append(
                GPUInfo(
                    index=int(_number(parts[0])),
                    name=parts[1],
                    vendor="nvidia",
                    utilisation=_number(parts[2]),
                    memory_used_mb=_number(parts[3]),
                    memory_total_mb=_number(parts[4]),
                    temperature_c=_number(parts[5]) if len(parts) > 5 else 0.0,
                    power_watts=_number(parts[6]) if len(parts) > 6 else 0.0,
                    fan_percent=_number(parts[7]) if len(parts) > 7 else 0.0,
                )
            )
        return out

    def close(self) -> None:
        if self._nvml is not None:
            with contextlib.suppress(Exception):  # best-effort shutdown
                self._nvml.nvmlShutdown()
            self._nvml = None


def _amd_cards() -> list[Path]:
    root = Path("/sys/class/drm")
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("card[0-9]") if (p / "device/gpu_busy_percent").exists())


def _sample_amd() -> list[GPUInfo]:
    out: list[GPUInfo] = []
    for index, card in enumerate(_amd_cards()):
        device = card / "device"
        info = GPUInfo(
            index=index, name=_read_text(device / "product_name") or "AMD GPU", vendor="amd"
        )
        info.utilisation = _read_number(device / "gpu_busy_percent")
        info.memory_used_mb = _read_number(device / "mem_info_vram_used") / 1024 / 1024
        info.memory_total_mb = _read_number(device / "mem_info_vram_total") / 1024 / 1024
        hwmon = next(iter((device / "hwmon").glob("hwmon*")), None)
        if hwmon is not None:
            info.temperature_c = _read_number(hwmon / "temp1_input") / 1000.0
            info.power_watts = _read_number(hwmon / "power1_average") / 1_000_000.0
        out.append(info)
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _read_number(path: Path) -> float:
    return _number(_read_text(path))


def _number(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _safe(fn: Any) -> float:
    try:
        return float(fn())
    except Exception:  # noqa: BLE001
        return 0.0
