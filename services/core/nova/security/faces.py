"""Local face detection and recognition via OpenCV's own YuNet/SFace models.

Detection and recognition are two small ONNX models OpenCV loads and runs
itself (cv2.FaceDetectorYN / cv2.FaceRecognizerSF) — no separate inference
runtime, no extra Python dependency beyond opencv-python-headless, which the
camera surface already requires. See docs/SETUP.md for where the model files
come from and `scripts/fetch_models.py` for fetching them.

Nothing here leaves the machine: a frame goes in, an embedding — a few
hundred floats describing the face, not a photo of it — comes out. What is
stored (`FaceStore`) is that embedding, not an image.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.errors import MissingDependency, MissingModel

DETECTOR_FILENAME = "face_detection_yunet_2023mar.onnx"
RECOGNISER_FILENAME = "face_recognition_sface_2021dec.onnx"

#: SFace's own documented cosine-similarity threshold for "same person" —
#: also security.match_threshold's default, exposed there so it can be
#: tuned per camera/lighting without a code change.
DEFAULT_MATCH_THRESHOLD = 0.363


@dataclass(slots=True)
class FaceObservation:
    """One detected face: where it is, and the embedding used to identify it."""

    bbox: tuple[int, int, int, int]  # x, y, width, height
    embedding: Any  # a (1, 128) float32 numpy array, SFace's own output shape


class FaceEngine:
    """Wraps YuNet (detection) and SFace (recognition/embedding)."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self._detector: Any = None
        self._recogniser: Any = None

    @property
    def loaded(self) -> bool:
        return self._detector is not None and self._recogniser is not None

    def load(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise MissingDependency("face recognition", "opencv-python-headless", "vision") from exc

        detector_path = self.models_dir / "face" / DETECTOR_FILENAME
        recogniser_path = self.models_dir / "face" / RECOGNISER_FILENAME
        if not detector_path.exists():
            raise MissingModel("face recognition", str(detector_path))
        if not recogniser_path.exists():
            raise MissingModel("face recognition", str(recogniser_path))

        # (320, 320) is a placeholder input size — setInputSize is called with
        # the real frame dimensions before every detect() below, since a
        # camera's actual resolution is not known until the first frame.
        self._detector = cv2.FaceDetectorYN.create(
            str(detector_path), "", (320, 320), 0.7, 0.3, 5000
        )
        self._recogniser = cv2.FaceRecognizerSF.create(str(recogniser_path), "")

    def observe(self, frame_bgr: Any) -> list[FaceObservation]:
        """Detect every face in a frame and compute its embedding."""
        if not self.loaded:
            return []

        height, width = frame_bgr.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(frame_bgr)
        if faces is None:
            return []

        observations = []
        for row in faces:
            bbox = (int(row[0]), int(row[1]), int(row[2]), int(row[3]))
            aligned = self._recogniser.alignCrop(frame_bgr, row)
            embedding = self._recogniser.feature(aligned)
            observations.append(FaceObservation(bbox=bbox, embedding=embedding))
        return observations


class FaceStore:
    """Enrolled faces: a name to one or more embeddings, in one JSON file.

    More than one embedding per name so a single enrollment photo is not the
    only reference forever — `learn_face` can be called again to add another
    angle or lighting condition without discarding what is already known.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, name: str, embedding: Any) -> None:
        data = self._read()
        data.setdefault(name, []).append(_to_list(embedding))
        self._write(data)

    def names(self) -> list[str]:
        return sorted(self._read())

    def forget(self, name: str) -> bool:
        data = self._read()
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    def match(self, embedding: Any, *, threshold: float) -> tuple[str, float] | None:
        """The best-matching enrolled name for an embedding, above `threshold`."""
        best: tuple[str, float] | None = None
        for name, vectors in self._read().items():
            for vector in vectors:
                score = _cosine_similarity(embedding, vector)
                if score >= threshold and (best is None or score > best[1]):
                    best = (name, score)
        return best

    def _read(self) -> dict[str, list[list[float]]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, list[list[float]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written atomically, same discipline as scripts/fetch_models.py —
        # a half-written faces.json is worse than a missing one.
        partial = self.path.with_suffix(".json.part")
        partial.write_text(json.dumps(data), encoding="utf-8")
        partial.replace(self.path)


def _to_list(embedding: Any) -> list[float]:
    import numpy as np

    return np.asarray(embedding, dtype=np.float32).reshape(-1).tolist()


def _cosine_similarity(a: Any, b: Any) -> float:
    import numpy as np

    left = np.asarray(a, dtype=np.float32).reshape(-1)
    right = np.asarray(b, dtype=np.float32).reshape(-1)
    denom = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denom == 0.0:
        return 0.0
    return float(np.dot(left, right) / denom)
