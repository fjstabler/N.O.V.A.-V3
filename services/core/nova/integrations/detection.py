"""On-device camera detection: motion, entirely locally.

The vision skill's ``look_at_camera`` sends a frame to a cloud model to *describe*
a scene; this is the fast, private complement that *detects* without anything
leaving the machine. Frame differencing in NumPy answers "is anything moving?"
in milliseconds, with no model, no key and no upload — so "tell me if someone
comes in" or "is the room still?" is a local computation, not an API call.

NumPy is imported lazily so the module still loads on a box without it; the
detector then reports the dependency as missing rather than failing at import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runtime.errors import MissingDependency


@dataclass(slots=True)
class MotionResult:
    """The outcome of comparing two frames."""

    moved: bool
    #: Fraction of the frame (0..1) whose pixels changed appreciably.
    score: float
    #: The fraction the score had to clear to count as motion.
    threshold: float

    def describe(self) -> str:
        percent = self.score * 100
        if self.moved:
            return f"Motion detected — {percent:.1f}% of the frame changed."
        return f"The scene is still ({percent:.1f}% change)."


class MotionDetector:
    """Detects movement between two frames by counting changed pixels.

    ``pixel_delta`` is how far a single pixel's brightness must shift to count as
    changed — high enough to shrug off sensor noise. ``min_area_fraction`` is how
    much of the frame must change before it is called motion, so a flickering
    highlight is not mistaken for someone walking in.
    """

    def __init__(self, *, pixel_delta: int = 25, min_area_fraction: float = 0.02) -> None:
        self.pixel_delta = pixel_delta
        self.min_area_fraction = min_area_fraction

    def score(self, a: Any, b: Any) -> float:
        """Fraction of pixels that changed by more than ``pixel_delta``."""
        np = _numpy()
        grey_a = self._greyscale(a, np)
        grey_b = self._greyscale(b, np)
        if grey_a.shape != grey_b.shape or grey_a.size == 0:
            # Mismatched or empty frames give no reliable reading — treat as still.
            return 0.0
        diff = np.abs(grey_a.astype(np.int16) - grey_b.astype(np.int16))
        changed = int(np.count_nonzero(diff > self.pixel_delta))
        return changed / diff.size

    def detect(self, a: Any, b: Any) -> MotionResult:
        score = self.score(a, b)
        return MotionResult(
            moved=score >= self.min_area_fraction,
            score=score,
            threshold=self.min_area_fraction,
        )

    def _greyscale(self, frame: Any, np: Any) -> Any:
        arr = np.asarray(frame)
        if arr.ndim == 3:
            # Average the colour channels; channel order (BGR vs RGB) is irrelevant
            # to how much a pixel changed.
            return arr[..., :3].mean(axis=2)
        return arr


def mean_brightness(frame: Any) -> float:
    """Average pixel value (0-255) — how bright the frame is overall."""
    np = _numpy()
    arr = np.asarray(frame)
    if arr.size == 0:
        return 0.0
    return float(arr.mean())


def looks_blank(frame: Any, *, threshold: float = 8.0) -> bool:
    """True for a near-black frame.

    Almost always means the camera index is wrong (nothing is actually being
    captured) or the lens is covered — not that the room is dark. Distinguishing
    this from real stillness is what stops "no movement" from being a lie when
    the camera is really returning nothing.
    """
    return mean_brightness(frame) < threshold


def _numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - numpy ships with the vision extra
        raise MissingDependency("motion detection", "numpy", "vision") from exc
    return np
