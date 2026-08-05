"""Local camera capture: grabbing a JPEG frame from a device on this machine.

Shared by the vision skill (the assistant describing what a camera sees) and
the display skill (showing a live snapshot in the desktop UI), so the OpenCV
capture and JPEG-encoding steps exist in one place rather than two.
"""

from __future__ import annotations

import io

from ..runtime.errors import MissingDependency, SkillError


def capture_camera_rgb(index: int) -> tuple[bytes, int, int]:
    """Grab one frame from a local camera device as raw RGB bytes."""
    try:
        import cv2
    except ImportError as exc:
        raise MissingDependency("camera", "opencv-python-headless", "vision") from exc

    capture = cv2.VideoCapture(index)
    try:
        if not capture.isOpened():
            raise SkillError(f"camera {index} could not be opened")
        # The first frame off a cold sensor is usually black; discard a few.
        for _ in range(5):
            capture.read()
        ok, frame = capture.read()
        if not ok or frame is None:
            raise SkillError("the camera did not return an image")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        return rgb.tobytes(), width, height
    finally:
        capture.release()


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
