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

#: Fallback when a detector is constructed without an explicit value (tests,
#: mainly) — config.schema.WakeWordSettings.consecutive_frames is what every
#: real detector actually gets built with.
DEFAULT_CONSECUTIVE_FRAMES = 3


class WakeWordDetector:
    """Scores audio frames against a wake word model."""

    def __init__(
        self,
        model: str,
        *,
        sensitivity: float = 0.55,
        cooldown: float = 1.5,
        consecutive_frames: int = DEFAULT_CONSECUTIVE_FRAMES,
        models_dir: Path | None = None,
    ) -> None:
        self.model_name = model
        self.sensitivity = sensitivity
        self.cooldown = cooldown
        self.consecutive_frames = consecutive_frames
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
            raise MissingDependency("wake word", "openwakeword", "wake") from exc

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
            # The single most common first-run failure is naming a phrase that
            # openWakeWord does not ship. Say so, and say what it does ship —
            # the raw library error names neither.
            available = _bundled_models()
            hint = f"available phrases: {', '.join(available)}" if available else ""
            message = str(exc).lower()
            if candidate.suffix != ".onnx" and (
                "download" in message
                or "no such file" in message
                or "not found" in message
                or "key" in message
            ):
                raise MissingModel(
                    "wake word",
                    f"'{self.model_name}' is not a bundled openWakeWord phrase. "
                    f"Either train one and point voice.wake.model at the .onnx file, "
                    f"or pick a bundled phrase in settings" + (f" — {hint}" if hint else ""),
                ) from exc
            if "download" in message or "no such file" in message:
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

        # `sensitivity` is documented, and shown in settings, as "higher =
        # easier to trigger" — the everyday sense of a sensitive detector.
        # The model's score needs to clear a bar to count as a hit, so the
        # bar itself has to move the opposite way: turning sensitivity up
        # lowers how confident the model has to be, not raises it. Comparing
        # the score against `sensitivity` directly here — instead of against
        # its complement — silently inverted the setting: maximum sensitivity
        # demanded the single strictest, most confident match possible.
        threshold = 1.0 - self.sensitivity
        if score < threshold:
            self._streak = 0
            return False

        self._streak += 1
        if self._streak < self.consecutive_frames:
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

    def unload(self) -> None:
        """Release the model. The detector then scores nothing."""
        self._model = None
        self._streak = 0
        self._peak = 0.0

    def update(
        self,
        *,
        sensitivity: float | None = None,
        cooldown: float | None = None,
        consecutive_frames: int | None = None,
    ) -> None:
        if sensitivity is not None:
            self.sensitivity = sensitivity
        if cooldown is not None:
            self.cooldown = cooldown
        if consecutive_frames is not None:
            self.consecutive_frames = consecutive_frames


def _bundled_models() -> list[str]:
    """Names openWakeWord has actually downloaded, for a useful error message."""
    try:
        import openwakeword

        models = getattr(openwakeword, "MODELS", None)
        if isinstance(models, dict) and models:
            return sorted(models)

        from pathlib import Path as _Path

        root = _Path(openwakeword.__file__).parent / "resources" / "models"
        return sorted(
            p.stem
            for p in root.glob("*.onnx")
            if not p.stem.startswith(("melspectrogram", "embedding", "silero"))
        )
    except Exception:  # noqa: BLE001 - this runs while reporting another error
        return []
