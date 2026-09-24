"""SystemdManager: the `server.managed_units` allowlist actually has to gate
`control()`, or configuring it is a no-op that silently doesn't restrict anything.

Shells out via `asyncio.create_subprocess_exec`, so it's faked the same way
`test_desktop.py` fakes it — no real systemd required.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.runtime.errors import IntegrationError, PermissionDenied
from nova.system import units as units_module
from nova.system.units import SystemdManager


class FakeProc:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class SpawnRecorder:
    def __init__(self, **proc_kwargs: Any) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._proc_kwargs = proc_kwargs

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProc:
        self.calls.append(argv)
        return FakeProc(**self._proc_kwargs)


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> SpawnRecorder:
    recorder = SpawnRecorder(
        stdout=b"Id=nginx.service\nLoadState=loaded\nActiveState=active\n"
        b"SubState=running\nDescription=nginx\n"
    )
    monkeypatch.setattr(units_module.asyncio, "create_subprocess_exec", recorder)
    return recorder


async def test_control_is_unrestricted_when_no_allowlist_is_configured(
    spawn: SpawnRecorder,
) -> None:
    manager = SystemdManager(managed_units=())
    await manager.control("restart", "nginx")
    assert spawn.calls  # the systemctl call actually happened


async def test_control_refuses_a_unit_outside_the_configured_allowlist(
    spawn: SpawnRecorder,
) -> None:
    manager = SystemdManager(managed_units=("myapp.service",))
    with pytest.raises(PermissionDenied, match="managed_units"):
        await manager.control("restart", "sshd")
    assert spawn.calls == []  # never even reached systemctl


async def test_control_permits_a_unit_inside_the_configured_allowlist(
    spawn: SpawnRecorder,
) -> None:
    manager = SystemdManager(managed_units=("nginx.service",))
    await manager.control("restart", "nginx")  # normalises to nginx.service
    assert spawn.calls


async def test_control_still_rejects_an_unsupported_action_first(spawn: SpawnRecorder) -> None:
    manager = SystemdManager(managed_units=("nginx.service",))
    with pytest.raises(IntegrationError, match="unsupported action"):
        await manager.control("poweroff", "nginx")
    assert spawn.calls == []
