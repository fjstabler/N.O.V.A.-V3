"""Wake word detection with openWakeWord.

Runs locally on CPU at a few percent of one core. Nothing leaves the machine
until the phrase fires — the microphone stream is consumed frame by frame,
scored, and discarded.

A raw threshold on the model score is too twitchy in a room with a television,
so detection requires the score to hold above the threshold for two consecutive
frames, and a cooldown suppresses the double-fire that otherwise follows every
successful trigger.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from ..runtime.errors import MissingDependency, MissingModel
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: Consecutive frames above threshold required to accept a detection.
CONSECUTIVE_FRAMES = 2


class WakeWordDetector:
    """Scores audio frames against a wake word model."""

    def __init__(
        self,
        model: str,
        *,
        sensitivity: float = 0.55,
        cooldown: float = 1.5,
        models_dir: Path | None = None,
    ) -> None:
        self.model_name = model
        self.sensitivity = sensitivity
        self.cooldown = cooldown
        self.models_dir = models_dir
        self._model: Any = None
        self._last_detection = 0.0
        self._streak = 0
        self._peak = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        """Load the model. Raises DegradedCapability subclasses if unavailable."""
        await asyncio.to_thread(self._load_sync)
        log.info("wake_word_ready", model=self.model_name, sensitivity=self.sensitivity)

    def _load_sync(self) -> None:
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise MissingDependency("wake word", "openwakeword", "voice") from exc

        candidate = Path(self.model_name)
        if candidate.suffix == ".onnx":
            if not candidate.is_absolute() and self.models_dir is not None:
                candidate = self.models_dir / candidate
            if not candidate.exists():
                raise MissingModel("wake word", str(candidate))
            paths = [str(candidate)]
        else:
            # A bundled model name, e.g. "hey_jarvis" or "alexa".
            paths = [self.model_name]

        try:
            self._model = Model(wakeword_models=paths, inference_framework="onnx")
        except Exception as exc:
            message = str(exc)
            if "download" in message.lower() or "no such file" in message.lower():
                raise MissingModel("wake word", self.model_name) from exc
            raise

    def process(self, frame: bytes) -> float:
        """Score one 80 ms frame. Returns the highest wake-word confidence."""
        if self._model is None:
            return 0.0
        try:
            import numpy

            samples = numpy.frombuffer(frame, dtype=numpy.int16)
            predictions = self._model.predict(samples)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not stop listening
            log.debug("wake_predict_failed", error=str(exc))
            return 0.0
        return max(predictions.values()) if predictions else 0.0

    def detected(self, frame: bytes) -> bool:
        """True when the wake phrase has just fired."""
        score = self.process(frame)
        self._peak = max(self._peak, score)

        if score < self.sensitivity:
            self._streak = 0
            return False

        self._streak += 1
        if self._streak < CONSECUTIVE_FRAMES:
            return False

        now = time.monotonic()
        if now - self._last_detection < self.cooldown:
            return False

        self._last_detection = now
        self._streak = 0
        log.info("wake_detected", score=round(score, 3))
        self._peak = 0.0
        return True

    def reset(self) -> None:
        """Clear internal buffers — call after speaking, to forget our own audio."""
        self._streak = 0
        self._peak = 0.0
        if self._model is not None:
            # Older openwakeword releases have no reset().
            with contextlib.suppress(Exception):
                self._model.reset()

    @property
    def peak_score(self) -> float:
        return self._peak

    def update(self, *, sensitivity: float | None = None, cooldown: float | None = None) -> None:
        if sensitivity is not None:
            self.sensitivity = sensitivity
        if cooldown is not None:
            self.cooldown = cooldown
