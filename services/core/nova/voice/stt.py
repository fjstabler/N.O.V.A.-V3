"""Speech to text with faster-whisper.

CTranslate2 under the hood, so a CUDA card gets float16 inference and a
CPU-only box gets int8 — both from the same model files. Device selection is
automatic unless the user pins it.

Transcription runs in a worker thread. The model is loaded once and reused; a
cold load of ``base`` takes a couple of seconds, which is why it happens at
service start rather than on the first utterance.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.errors import MissingDependency
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: Whisper hallucinates these on silence; drop them rather than acting on them.
_HALLUCINATIONS = frozenset(
    {
        "",
        ".",
        "you",
        "thank you.",
        "thanks for watching!",
        "thank you for watching.",
        "please subscribe.",
        "bye.",
        "[blank_audio]",
        "[silence]",
        "(silence)",
        "...",
    }
)


@dataclass(slots=True)
class Transcript:
    text: str
    language: str = ""
    duration_ms: int = 0
    audio_ms: int = 0
    confidence: float = 0.0

    @property
    def usable(self) -> bool:
        cleaned = self.text.strip().lower()
        return bool(cleaned) and cleaned not in _HALLUCINATIONS and len(cleaned) > 1


class Transcriber:
    """Wraps a faster-whisper model."""

    def __init__(
        self,
        *,
        model_size: str = "base",
        device: str = "auto",
        compute_type: str = "auto",
        language: str = "en",
        beam_size: int = 1,
        models_dir: Path | None = None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.models_dir = models_dir
        self._model: Any = None
        self._resolved_device = ""

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def resolved_device(self) -> str:
        return self._resolved_device

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)
        log.info(
            "transcriber_ready",
            model=self.model_size,
            device=self._resolved_device,
            compute=self.compute_type,
        )

    def _load_sync(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise MissingDependency("speech recognition", "faster-whisper", "voice") from exc

        device = self.device if self.device != "auto" else _detect_device()
        compute = self.compute_type
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"

        try:
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute,
                download_root=str(self.models_dir / "whisper") if self.models_dir else None,
            )
        except Exception as exc:
            # A CUDA build without a usable card is the common failure; CPU works.
            if device == "cuda":
                log.warning("cuda_unavailable_for_stt", error=str(exc)[:200])
                self._model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.models_dir / "whisper") if self.models_dir else None,
                )
                device, compute = "cpu", "int8"
            else:
                raise
        self._resolved_device = device
        self.compute_type = compute

    async def transcribe(self, audio: bytes) -> Transcript:
        """Transcribe 16 kHz mono 16-bit PCM.

        Bounded by a timeout because a wedged inference backend would otherwise
        hang the turn forever with the assistant stuck in THINKING and nothing
        in the log to say why. The most common cause is a CUDA build meeting a
        cuDNN it does not like.

        The worker thread cannot be killed — it is left to finish or die on its
        own — but the event loop is freed, so the assistant recovers, reports,
        and stays usable.
        """
        if self._model is None:
            return Transcript(text="")

        seconds = len(audio) / (16000 * 2)
        budget = max(30.0, seconds * 6)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_sync, audio), timeout=budget
            )
        except TimeoutError:
            log.error(
                "transcription_timed_out",
                seconds=round(seconds, 1),
                budget=round(budget),
                device=self._resolved_device,
                hint="a CUDA/cuDNN mismatch is the usual cause; "
                "set voice.stt.device to 'cpu' to rule it out",
            )
            return Transcript(text="")

    def _transcribe_sync(self, audio: bytes) -> Transcript:
        import numpy

        started = time.perf_counter()
        samples = numpy.frombuffer(audio, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        if samples.size == 0:
            return Transcript(text="")

        segments, info = self._model.transcribe(
            samples,
            language=None if self.language == "auto" else self.language,
            beam_size=self.beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,  # stops one bad turn poisoning the next
        )
        parts: list[str] = []
        probabilities: list[float] = []
        for segment in segments:
            parts.append(segment.text)
            if segment.avg_logprob is not None:
                probabilities.append(float(segment.avg_logprob))

        text = " ".join(p.strip() for p in parts).strip()
        return Transcript(
            text=text,
            language=getattr(info, "language", "") or "",
            duration_ms=int((time.perf_counter() - started) * 1000),
            audio_ms=int(samples.size / 16000 * 1000),
            confidence=_logprob_to_confidence(probabilities),
        )

    def close(self) -> None:
        self._model = None


def _detect_device() -> str:
    """Prefer CUDA when a usable card is present."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001 - no ctranslate2, or no driver
        pass
    return "cpu"


def _logprob_to_confidence(logprobs: list[float]) -> float:
    if not logprobs:
        return 0.0
    import math

    return round(math.exp(sum(logprobs) / len(logprobs)), 3)
