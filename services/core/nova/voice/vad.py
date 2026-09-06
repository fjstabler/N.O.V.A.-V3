"""Utterance endpointing.

Decides when the user has stopped talking. WebRTC's VAD does the per-frame
speech/silence call when available; otherwise an adaptive energy gate takes over,
which is less precise but keeps voice usable without the extra wheel.

The state machine is deliberately forgiving at the start (wait up to a few
seconds for speech to begin) and firm at the end (close after a configurable run
of silence), because the failure modes are asymmetric: cutting someone off is
far more annoying than a beat of extra silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..runtime.logging import get_logger

log = get_logger(__name__)

FRAME_MS = 80
#: WebRTC only accepts 10/20/30 ms frames, so an 80 ms block is split into 20 ms slices.
VAD_SLICE_MS = 20


class EndpointState(StrEnum):
    WAITING = "waiting"
    SPEAKING = "speaking"
    DONE = "done"
    TIMEOUT = "timeout"


@dataclass(slots=True)
class EndpointResult:
    state: EndpointState
    speech_ms: int = 0
    silence_ms: int = 0


class Endpointer:
    """Accumulates frames until the utterance is complete."""

    def __init__(
        self,
        *,
        silence_ms: int = 800,
        max_utterance_seconds: float = 30.0,
        aggressiveness: int = 2,
        start_timeout_seconds: float = 6.0,
    ) -> None:
        self.silence_ms = silence_ms
        self.max_utterance_ms = int(max_utterance_seconds * 1000)
        self.start_timeout_ms = int(start_timeout_seconds * 1000)
        self._vad: Any = self._make_vad(aggressiveness)
        self._energy_floor: float | None = None
        self.reset()

    def _make_vad(self, aggressiveness: int) -> Any:
        try:
            import webrtcvad

            return webrtcvad.Vad(max(0, min(3, aggressiveness)))
        except ImportError:
            log.info("webrtcvad_unavailable", fallback="energy gate")
            return None

    def reset(self) -> None:
        self._state = EndpointState.WAITING
        self._elapsed_ms = 0
        self._speech_ms = 0
        self._silence_ms = 0
        self._energy_floor = None

    @property
    def state(self) -> EndpointState:
        return self._state

    def feed(self, frame: bytes) -> EndpointResult:
        """Push one 80 ms frame and get the current endpointing decision."""
        self._elapsed_ms += FRAME_MS
        is_speech = self._is_speech(frame)

        if is_speech:
            self._speech_ms += FRAME_MS
            self._silence_ms = 0
            if self._state is EndpointState.WAITING:
                self._state = EndpointState.SPEAKING
        else:
            self._silence_ms += FRAME_MS

        if self._state is EndpointState.WAITING:
            if self._elapsed_ms >= self.start_timeout_ms:
                self._state = EndpointState.TIMEOUT
        elif self._state is EndpointState.SPEAKING:
            if self._silence_ms >= self.silence_ms:
                self._state = EndpointState.DONE
            elif self._elapsed_ms >= self.max_utterance_ms:
                log.info("utterance_length_limit", ms=self._elapsed_ms)
                self._state = EndpointState.DONE

        return EndpointResult(self._state, self._speech_ms, self._silence_ms)

    def _is_speech(self, frame: bytes) -> bool:
        if self._vad is not None:
            return self._webrtc_speech(frame)
        return self._energy_speech(frame)

    def _webrtc_speech(self, frame: bytes) -> bool:
        slice_bytes = int(16000 * VAD_SLICE_MS / 1000) * 2
        votes = 0
        slices = 0
        for offset in range(0, len(frame) - slice_bytes + 1, slice_bytes):
            chunk = frame[offset : offset + slice_bytes]
            slices += 1
            try:
                if self._vad.is_speech(chunk, 16000):
                    votes += 1
            except Exception:  # noqa: BLE001 - malformed slice length
                continue
        # A single positive slice in an 80 ms block is usually a transient.
        return slices > 0 and votes >= 2

    def _energy_speech(self, frame: bytes) -> bool:
        """Adaptive noise gate used when webrtcvad is not installed."""
        energy = _frame_energy(frame)
        if self._energy_floor is None:
            self._energy_floor = energy
            return False
        # Track the noise floor upward slowly and downward quickly.
        if energy < self._energy_floor:
            self._energy_floor = self._energy_floor * 0.9 + energy * 0.1
        else:
            self._energy_floor = self._energy_floor * 0.995 + energy * 0.005
        return energy > max(self._energy_floor * 3.0, 0.008)

    @property
    def had_speech(self) -> bool:
        # Under ~200 ms is a cough or a door, not an utterance.
        return self._speech_ms >= 200


def _frame_energy(frame: bytes) -> float:
    try:
        import numpy
    except ImportError:
        return 0.0
    samples = numpy.frombuffer(frame, dtype=numpy.int16).astype(numpy.float32) / 32768.0
    if samples.size == 0:
        return 0.0
    return float(numpy.sqrt(numpy.mean(samples**2)))
