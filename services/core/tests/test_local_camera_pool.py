"""LocalCameraPool: a device stays open across repeated snapshots.

Regression: the camera surface polls every couple of seconds, and the first
version opened and released a real USB webcam on every single poll. Opening
one from scratch can itself take longer than the poll interval — negotiating
format and resolution is not instant — so a second open request could arrive
before the first had released the device. V4L2 only allows one owner, so
both attempts failed, every time, forever: "camera 0 could not be opened".
The pool exists to open a device once and read from it repeatedly instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nova.integrations import local_camera
from nova.integrations.local_camera import LocalCameraPool


class FakeCapture:
    def __init__(self) -> None:
        self.reads = 0
        self.released = False

    def read(self) -> tuple[bool, str]:
        self.reads += 1
        return True, "frame"

    def release(self) -> None:
        self.released = True


@pytest.fixture()
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], dict[int, FakeCapture]]:
    opens: list[int] = []
    captures: dict[int, FakeCapture] = {}

    def fake_open(index: int) -> FakeCapture:
        opens.append(index)
        capture = FakeCapture()
        captures[index] = capture
        return capture

    def fake_read(capture: FakeCapture) -> tuple[bytes, int, int]:
        capture.read()
        return b"rgb-bytes", 4, 4

    def fake_encode(raw: bytes, width: int, height: int, **_: Any) -> bytes:
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(local_camera, "_open_camera", fake_open)
    monkeypatch.setattr(local_camera, "_read_frame", fake_read)
    monkeypatch.setattr(local_camera, "encode_jpeg", fake_encode)
    return opens, captures


async def test_repeated_snapshots_reuse_the_same_open_handle(
    fake_backend: tuple[list[int], dict[int, FakeCapture]],
) -> None:
    opens, captures = fake_backend
    pool = LocalCameraPool()

    for _ in range(3):
        image = await pool.snapshot_jpeg(0)
        assert image == b"\xff\xd8jpeg"

    assert opens == [0]  # opened once, not once per snapshot
    assert captures[0].reads == 3  # but a fresh frame every time


async def test_concurrent_snapshots_on_the_same_index_do_not_collide(
    fake_backend: tuple[list[int], dict[int, FakeCapture]],
) -> None:
    """The exact failure mode being fixed: two requests for the same camera
    landing close together must not both try to open the device."""
    opens, _ = fake_backend
    pool = LocalCameraPool()

    results = await asyncio.gather(*[pool.snapshot_jpeg(0) for _ in range(8)])

    assert opens == [0]
    assert all(image == b"\xff\xd8jpeg" for image in results)


async def test_different_indices_get_their_own_handle(
    fake_backend: tuple[list[int], dict[int, FakeCapture]],
) -> None:
    opens, _ = fake_backend
    pool = LocalCameraPool()

    await pool.snapshot_jpeg(0)
    await pool.snapshot_jpeg(1)
    await pool.snapshot_jpeg(0)

    assert sorted(set(opens)) == [0, 1]
    assert opens.count(0) == 1  # index 0 was not reopened on the third call


async def test_an_idle_handle_is_released(
    fake_backend: tuple[list[int], dict[int, FakeCapture]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The camera's own indicator light should not stay on long after
    whatever was showing it on screen has been closed."""
    _, captures = fake_backend
    clock = {"now": 0.0}
    monkeypatch.setattr(local_camera.time, "monotonic", lambda: clock["now"])
    pool = LocalCameraPool()

    await pool.snapshot_jpeg(0)
    assert 0 in pool._open

    clock["now"] += local_camera.IDLE_TIMEOUT_SECONDS + 1
    await pool._reap_idle()

    assert 0 not in pool._open
    assert captures[0].released is True


async def test_a_handle_mid_read_is_never_reaped_out_from_under_it(
    fake_backend: tuple[list[int], dict[int, FakeCapture]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusually slow read must not have its handle released mid-flight —
    `last_used` is stale (not yet bumped) for exactly as long as the read is
    in progress, so a reap sweep landing in that window has to recognise the
    handle is still in active use rather than treating it as idle."""
    _, captures = fake_backend
    clock = {"now": 0.0}
    monkeypatch.setattr(local_camera.time, "monotonic", lambda: clock["now"])
    started = asyncio.Event()
    release_read = asyncio.Event()

    async def slow_to_thread(fn: Any, *args: Any) -> Any:
        # Only the read is slow here — opening the device (also routed
        # through to_thread) must stay instant, or "started" would fire
        # before the handle even exists in the pool.
        if fn is local_camera._read_frame:
            started.set()
            await release_read.wait()
        return fn(*args)

    monkeypatch.setattr(local_camera.asyncio, "to_thread", slow_to_thread)
    pool = LocalCameraPool()

    task = asyncio.create_task(pool.snapshot_jpeg(0))
    await started.wait()

    clock["now"] += local_camera.IDLE_TIMEOUT_SECONDS + 1
    await pool._reap_idle()
    assert 0 in pool._open  # still mid-read; must not have been released

    release_read.set()
    await task

    assert captures[0].released is False


async def test_release_all_closes_every_open_handle(
    fake_backend: tuple[list[int], dict[int, FakeCapture]],
) -> None:
    _, captures = fake_backend
    pool = LocalCameraPool()

    await pool.snapshot_jpeg(0)
    await pool.snapshot_jpeg(1)
    await pool.release_all()

    assert captures[0].released is True
    assert captures[1].released is True
    assert pool._open == {}
