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

#: What to answer to when the configured phrase has no model.
#:
#: openWakeWord ships four phrases, and "hey nova" is not among them — training
#: one is a real job, so the default names a phrase that cannot load on a fresh
#: install. Refusing to listen at all is the worst of the options: it leaves an
#: assistant that cannot be spoken to, and the reason is a line in a log nobody
#: is reading. Substituting a phrase that does exist at least leaves a working
#: microphone, provided it is impossible to miss which phrase is live.
#:
#: Ordered by how well each suits a personal assistant.
FALLBACK_PHRASES = ("hey_jarvis", "hey_mycroft", "hey_rhasspy", "alexa")


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
        #: The phrase actually loaded. Differs from `model_name` when the
        #: configured one had no model and a fallback was substituted.
        self.active_model = model
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
            # A path the user chose themselves is not something to second-guess.
            if not candidate.is_absolute() and self.models_dir is not None:
                candidate = self.models_dir / candidate
            if not candidate.exists():
                raise MissingModel("wake word", str(candidate))
            self._model = Model(wakeword_models=[str(candidate)], inference_framework="onnx")
            self.active_model = self.model_name
            return

        # A bundled name, e.g. "hey_jarvis". Ask the library what it has rather
        # than reading the failure: openWakeWord says "Could not find pretrained
        # model for model name 'x'", which matched none of the phrases the old
        # code searched for, so the helpful branch never actually ran.
        available = _bundled_models()
        if _is_available(self.model_name, available):
            self._model = Model(wakeword_models=[self.model_name], inference_framework="onnx")
            self.active_model = self.model_name
            return

        substitute = self._load_fallback(Model, available)
        if substitute is None:
            hint = f"available phrases: {', '.join(available)}" if available else ""
            raise MissingModel(
                "wake word",
                f"'{self.model_name}' is not a bundled openWakeWord phrase, and no "
                f"fallback loaded either. Either train one and point voice.wake.model "
                f"at the .onnx file, or pick a bundled phrase in settings"
                + (f" — {hint}" if hint else ""),
            )

        self.active_model = substitute
        log.warning(
            "wake_word_substituted",
            requested=self.model_name,
            listening_for=substitute,
            reason=f"openWakeWord ships no '{self.model_name}' model",
            remedy="train one and point voice.wake.model at its .onnx file, or set "
            "voice.wake.model to a bundled phrase to silence this",
        )

    def _load_fallback(self, model_factory: Any, available: list[str]) -> str | None:
        """Load the first bundled phrase that works, or None if none do."""
        for phrase in FALLBACK_PHRASES:
            if not _is_available(phrase, available):
                continue
            try:
                self._model = model_factory(wakeword_models=[phrase], inference_framework="onnx")
            except Exception:  # noqa: BLE001 - try the next one
                continue
            return phrase
        return None

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
        """Highest confidence since the last reset.

        The difference between "nothing is reaching the microphone" and "the
        phrase is heard but never quite clears the bar" is invisible from the
        outside, and the two need opposite fixes.
        """
        return self._peak

    def reset_peak(self) -> None:
        self._peak = 0.0

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


def _is_available(phrase: str, available: list[str]) -> bool:
    """Whether openWakeWord ships a model for `phrase`.

    The listing carries version suffixes — `hey_jarvis_v0.1` — while the loader
    wants the bare name, so this matches on the stem. An empty listing means the
    inventory could not be read, not that nothing is installed; in that case say
    yes and let the load attempt be the judge.
    """
    if not available:
        return True
    return any(name == phrase or name.startswith(f"{phrase}_") for name in available)


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
