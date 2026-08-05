"""Local camera capture: grabbing a JPEG frame from a device on this machine.

Shared by the vision skill (the assistant describing what a camera sees) and
the display skill (showing a live snapshot in the desktop UI), so the OpenCV
capture and JPEG-encoding steps exist in one place rather than two.

A one-off `look_at_camera` call and a camera surface polled every couple of
seconds have different needs here. The one-off path (`capture_camera_rgb`)
opens the device, reads a frame, and releases it — correct for something
asked once. Reopening a real USB webcam from scratch on every poll is not:
negotiating format/resolution can itself take longer than the poll interval,
so a second open can arrive before the first has released the device, and
V4L2 only allows one owner — both fail. `LocalCameraPool` exists for that
case, keeping the device open across repeated snapshots instead, the same
principle `voice/audio.py` already uses for the microphone.
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any

from ..runtime.errors import MissingDependency, SkillError
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: How long a pooled camera handle may sit unused before it is released — long
#: enough to comfortably outlast gaps between polls in one viewing session,
#: short enough that the camera's own indicator light does not stay lit for
#: minutes after the surface showing it has been closed.
IDLE_TIMEOUT_SECONDS = 8.0


def capture_camera_rgb(index: int) -> tuple[bytes, int, int]:
    """Grab one frame from a local camera device as raw RGB bytes."""
    capture = _open_camera(index)
    try:
        frame = _read_frame_bgr(capture)
        return _bgr_to_rgb_bytes(frame)
    finally:
        capture.release()


def _open_camera(index: int) -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise MissingDependency("camera", "opencv-python-headless", "vision") from exc

    capture = cv2.VideoCapture(index)
    if not capture.isOpened():
        raise SkillError(f"camera {index} could not be opened")
    # The first frame off a cold sensor is usually black; discard a few. Only
    # needed once, right after opening — a handle that is already streaming
    # does not need this repeated on every subsequent read.
    for _ in range(5):
        capture.read()
    return capture


def _read_frame_bgr(capture: Any) -> Any:
    """Read one raw frame as a BGR numpy array — OpenCV's own native layout,
    which is what both JPEG encoding and face detection start from."""
    ok, frame = capture.read()
    if not ok or frame is None:
        raise SkillError("the camera did not return an image")
    return frame


def _bgr_to_rgb_bytes(frame: Any) -> tuple[bytes, int, int]:
    import cv2

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    return rgb.tobytes(), width, height


def encode_jpeg(
    raw: bytes,
    width: int,
    height: int,
    *,
    mode: str = "RGB",
    max_edge: int = 1280,
    quality: int = 82,
) -> bytes:
    """Encode a raw pixel buffer as a size-capped JPEG."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise MissingDependency("image encoding", "pillow", "vision") from exc

    image = Image.frombytes(mode, (width, height), raw)
    longest = max(image.size)
    if longest > max_edge:
        scale = max_edge / longest
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS
        )
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def capture_camera_jpeg(index: int, *, max_edge: int = 1280, quality: int = 82) -> bytes:
    """Grab one frame and encode it, for a caller that just wants JPEG bytes."""
    raw, width, height = capture_camera_rgb(index)
    return encode_jpeg(raw, width, height, max_edge=max_edge, quality=quality)


class _OpenCamera:
    def __init__(self, capture: Any) -> None:
        self.capture = capture
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()


class LocalCameraPool:
    """Camera handles kept open across repeated snapshots, one per index.

    A per-index lock serialises reads on that handle — cv2's own read is not
    safe to call from two threads at once — and a handle idle for longer than
    `IDLE_TIMEOUT_SECONDS` is released rather than kept open indefinitely.
    """

    def __init__(self) -> None:
        self._open: dict[int, _OpenCamera] = {}
        self._pool_lock = asyncio.Lock()

    async def snapshot_jpeg(self, index: int, *, max_edge: int = 1280, quality: int = 82) -> bytes:
        frame = await self.read_bgr(index)
        raw, width, height = await asyncio.to_thread(_bgr_to_rgb_bytes, frame)
        return encode_jpeg(raw, width, height, max_edge=max_edge, quality=quality)

    async def read_bgr(self, index: int) -> Any:
        """Read one raw BGR frame — what face detection needs, unlike
        `snapshot_jpeg`'s already-encoded bytes."""
        entry = await self._acquire(index)
        async with entry.lock:
            frame = await asyncio.to_thread(_read_frame_bgr, entry.capture)
            entry.last_used = time.monotonic()
        await self._reap_idle()
        return frame

    async def _acquire(self, index: int) -> _OpenCamera:
        async with self._pool_lock:
            entry = self._open.get(index)
            if entry is not None:
                return entry
            capture = await asyncio.to_thread(_open_camera, index)
            entry = _OpenCamera(capture)
            self._open[index] = entry
            log.info("local_camera_opened", index=index)
            return entry

    async def _reap_idle(self) -> None:
        async with self._pool_lock:
            now = time.monotonic()
            # `lock.locked()` excludes a handle a slow read is still using —
            # its `last_used` is not bumped until that read finishes, so
            # without this an unusually slow read could look idle and be
            # released out from under the request still waiting on it.
            idle = [
                i
                for i, e in self._open.items()
                if not e.lock.locked() and now - e.last_used > IDLE_TIMEOUT_SECONDS
            ]
            for i in idle:
                entry = self._open.pop(i)
                await asyncio.to_thread(entry.capture.release)
                log.info("local_camera_released", index=i)

    async def release_all(self) -> None:
        async with self._pool_lock:
            for entry in self._open.values():
                await asyncio.to_thread(entry.capture.release)
            self._open.clear()


#: One pool for the process — a camera surface polling every couple of
#: seconds is the only caller with a reason to keep a handle open between
#: requests, so there is exactly one of these rather than one per request.
local_camera_pool = LocalCameraPool()
