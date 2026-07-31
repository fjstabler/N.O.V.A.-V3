"""Speech synthesis with Kokoro.

Kokoro is an 82M-parameter open-weight model that runs on ONNX Runtime, so it is
fast on CPU and free — which is what keeps N.O.V.A. off commercial speech APIs.

Text arrives from the orchestrator as it is generated, so synthesis is per
sentence rather than per reply. Two things matter for that to sound right:
normalising anything the model wrote that would be read literally (markdown,
percent signs, unit abbreviations), and keeping a small cache of short stock
phrases so acknowledgements are instant.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..runtime.errors import MissingDependency, MissingModel
from ..runtime.logging import get_logger

log = get_logger(__name__)

MODEL_FILENAME = "kokoro-v1.0.onnx"
VOICES_FILENAME = "voices-v1.0.bin"

#: Short phrases worth keeping synthesised — acknowledgements repeat constantly.
CACHE_SIZE = 24
CACHE_MAX_CHARS = 60

_MARKDOWN = re.compile(r"[*_`#>]|\[(.*?)]\(.*?\)")
_WHITESPACE = re.compile(r"\s+")
_ABBREVIATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(\d)\s*%"), r"\1 percent"),
    (re.compile(r"(\d)\s*°C\b"), r"\1 degrees"),
    (re.compile(r"(\d)\s*°F\b"), r"\1 degrees"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*GB\b", re.I), r"\1 gigabytes"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*MB\b", re.I), r"\1 megabytes"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*TB\b", re.I), r"\1 terabytes"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*KB\b", re.I), r"\1 kilobytes"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*ms\b"), r"\1 milliseconds"),
    (re.compile(r"\bCPU\b"), "C P U"),
    (re.compile(r"\bGPU\b"), "G P U"),
    (re.compile(r"\bRAM\b"), "ram"),
    (re.compile(r"\bSSH\b"), "S S H"),
    (re.compile(r"\bIP\b"), "I P"),
    (re.compile(r"\bAPI\b"), "A P I"),
    (re.compile(r"\bN\.O\.V\.A\.\b"), "Nova"),
)


class Synthesiser:
    """Kokoro text-to-speech."""

    def __init__(
        self,
        *,
        voice: str = "af_sarah",
        speed: float = 1.05,
        models_dir: Path | None = None,
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.models_dir = models_dir or Path.cwd()
        self._engine: Any = None
        self._sample_rate = 24000
        self._cache: OrderedDict[str, tuple[Any, int]] = OrderedDict()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)
        log.info("synthesiser_ready", voice=self.voice, rate=self._sample_rate)

    def _load_sync(self) -> None:
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise MissingDependency("speech synthesis", "kokoro-onnx", "voice") from exc

        model_path = self.models_dir / "kokoro" / MODEL_FILENAME
        voices_path = self.models_dir / "kokoro" / VOICES_FILENAME
        if not model_path.exists():
            raise MissingModel("speech synthesis", str(model_path))
        if not voices_path.exists():
            raise MissingModel("speech synthesis", str(voices_path))

        self._engine = Kokoro(str(model_path), str(voices_path))

    def available_voices(self) -> list[str]:
        if self._engine is None:
            return []
        try:
            return sorted(self._engine.get_voices())
        except Exception:  # noqa: BLE001
            return []

    async def synthesise(self, text: str) -> tuple[Any, int] | None:
        """Return ``(float32 samples, sample_rate)`` for ``text``."""
        if self._engine is None:
            return None
        cleaned = normalise_for_speech(text)
        if not cleaned:
            return None

        cached = self._cache.get(cleaned)
        if cached is not None:
            self._cache.move_to_end(cleaned)
            return cached

        result = await asyncio.to_thread(self._synthesise_sync, cleaned)
        if result is not None and len(cleaned) <= CACHE_MAX_CHARS:
            self._cache[cleaned] = result
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    def _synthesise_sync(self, text: str) -> tuple[Any, int] | None:
        try:
            samples, sample_rate = self._engine.create(
                text, voice=self.voice, speed=self.speed, lang="en-us"
            )
        except Exception as exc:  # noqa: BLE001 - an unpronounceable string is not fatal
            log.warning("synthesis_failed", error=str(exc)[:200], text=text[:80])
            return None
        self._sample_rate = int(sample_rate)
        return samples, int(sample_rate)

    def update(self, *, voice: str | None = None, speed: float | None = None) -> None:
        if voice is not None and voice != self.voice:
            self.voice = voice
            self._cache.clear()
        if speed is not None and speed != self.speed:
            self.speed = speed
            self._cache.clear()

    def close(self) -> None:
        self._engine = None
        self._cache.clear()


def normalise_for_speech(text: str) -> str:
    """Rewrite text so it is read aloud the way a person would say it."""
    cleaned = _MARKDOWN.sub(r"\1", text)
    for pattern, replacement in _ABBREVIATIONS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = cleaned.replace("→", " to ").replace("—", ", ").replace("…", ".")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned
