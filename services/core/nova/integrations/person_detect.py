"""On-device person detection — "is anyone in the room" done properly.

Face detection only sees a face pointed at the camera in good light; this sees
*whole people*, at any angle, in most light, which is what "is someone here"
actually needs. It runs a small YOLO model locally through Ultralytics — nothing
is uploaded, and no face is recognised or stored, so it answers presence without
identity.

Ultralytics is an optional extra: absent, the detector reports the dependency as
missing rather than failing, and the rest of the camera surface carries on. The
model file downloads itself on first use and is cached, so there is nothing to
fetch by hand. Ultralytics is imported lazily and the model loaded once, so the
cost lands on the first check, not at import.
"""

from __future__ import annotations

from typing import Any

from ..runtime.errors import MissingDependency


class PersonDetector:
    """Detects people in a frame with a local YOLO model (person = COCO class 0)."""

    #: COCO index for "person"; the only class we ask the model to return.
    PERSON_CLASS = 0

    def __init__(self, *, model: str = "yolov8n.pt", confidence: float = 0.4) -> None:
        self.model_name = model
        self.confidence = confidence
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise MissingDependency("person detection", "ultralytics", "person") from exc
        # Ultralytics fetches and caches the weights itself on first construction.
        self._model = YOLO(self.model_name)
        return self._model

    def detect(self, frame_bgr: Any) -> list[float]:
        """Confidences of every person found above the threshold (blocking)."""
        model = self._ensure_model()
        results = model.predict(
            frame_bgr,
            classes=[self.PERSON_CLASS],
            conf=self.confidence,
            verbose=False,
        )
        return person_confidences(results)


def person_confidences(results: Any) -> list[float]:
    """Pull the per-detection confidences out of an Ultralytics result.

    Isolated from the model call so the parsing is testable with a stand-in
    result object and no torch, no weights and no camera.
    """
    confidences: list[float] = []
    for result in results:
        boxes = getattr(result, "boxes", None)
        conf = getattr(boxes, "conf", None) if boxes is not None else None
        if conf is None:
            continue
        confidences.extend(float(c) for c in conf.tolist())
    return confidences


def describe_people(confidences: list[float]) -> str:
    """A spoken answer to 'is anyone here' from the detected confidences."""
    count = len(confidences)
    if count == 0:
        return "No — the room looks empty."
    people = "person" if count == 1 else "people"
    return f"Yes — I can see {count} {people}."
