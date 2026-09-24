"""Audio capture and playback.

One always-open input stream feeds every consumer (wake word, endpointer,
transcription) through a fan-out queue. Opening and closing a device per
utterance is what makes most voice assistants clip the first syllable, so the
stream stays up for the life of the process and consumers are switched instead.

The PortAudio callback runs on a real-time thread: it must never block, never
allocate unpredictably, and never touch the event loop directly. It does exactly
one thing — copy the frame into a bounded queue — and drops frames rather than
stalling if a consumer falls behind.
"""

from __future__ import annotations

import asyncio
import base64
import io
import queue
import threading
import wave
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..runtime.errors import MissingDependency
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: openWakeWord and Whisper both expect 16 kHz mono.
SAMPLE_RATE = 16000
#: 80 ms at 16 kHz — the frame size openWakeWord is trained on.
FRAME_SAMPLES = 1280
BYTES_PER_SAMPLE = 2
#: One wake-word frame on the wire.
FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE


@runtime_checkable
class MicrophoneSource(Protocol):
    """Everything the listen loop needs from a microphone.

    Named as a protocol rather than a base class because the two
    implementations share no machinery: one copies frames off a PortAudio
    real-time thread, the other takes them off a WebSocket (see
    ``voice/remote.py``). What they owe the loop is this surface and the
    guarantee that :meth:`frames` yields 80 ms of 16 kHz mono at a time.
    """

    #: Multiplier applied to every frame before it reaches a consumer.
    gain: float

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def frames(self) -> Any:
        """Async iterator of 16-bit PCM frames; may yield b"" as an idle tick."""
        ...

    def read_preroll(self) -> bytes: ...

    def drain(self) -> None: ...

    def set_muted(self, muted: bool) -> None: ...

    @property
    def level(self) -> float: ...


@dataclass(slots=True)
class AudioDevice:
    index: int
    name: str
    channels: int
    default: bool
    kind: str  # "input" | "output"

    def as_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "channels": self.channels,
            "default": self.default,
            "kind": self.kind,
        }


def _require_sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        # OSError here means PortAudio itself is missing (apt install libportaudio2).
        raise MissingDependency("audio", "sounddevice", "voice") from exc
    return sounddevice


def list_devices() -> list[AudioDevice]:
    """Enumerate audio devices; returns an empty list if PortAudio is absent."""
    try:
        sounddevice = _require_sounddevice()
        defaults = sounddevice.default.device
    except MissingDependency:
        return []
    out: list[AudioDevice] = []
    try:
        for index, info in enumerate(sounddevice.query_devices()):
            if info["max_input_channels"] > 0:
                out.append(
                    AudioDevice(
                        index,
                        info["name"],
                        info["max_input_channels"],
                        index == defaults[0],
                        "input",
                    )
                )
            if info["max_output_channels"] > 0:
                out.append(
                    AudioDevice(
                        index,
                        info["name"],
                        info["max_output_channels"],
                        index == defaults[1],
                        "output",
                    )
                )
    except Exception as exc:  # noqa: BLE001 - a broken ALSA config should not crash us
        log.warning("device_enumeration_failed", error=str(exc))
    return out


def resolve_device(name: str, kind: str) -> int | None:
    """Map a configured device-name substring to a PortAudio index."""
    if not name.strip():
        return None
    needle = name.strip().lower()
    for device in list_devices():
        if device.kind == kind and needle in device.name.lower():
            return device.index
    log.warning("audio_device_not_found", name=name, kind=kind)
    return None


class AudioInput:
    """Continuous microphone capture with a pre-roll ring buffer."""

    #: Seconds of audio retained before the wake word fires, so the utterance
    #: that follows it is never clipped at the front.
    PREROLL_SECONDS = 1.0

    def __init__(self, *, device: str = "", gain: float = 1.0) -> None:
        self.device_name = device
        self.gain = gain
        self._stream: Any = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._preroll: deque[bytes] = deque(
            maxlen=int(self.PREROLL_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
        )
        self._level = 0.0
        self._lock = threading.Lock()
        self._muted = False
        self._dropped = 0

    def start(self) -> None:
        sounddevice = _require_sounddevice()
        device_index = resolve_device(self.device_name, "input")
        self._stream = sounddevice.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            device=device_index,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()
        log.info("microphone_open", device=self.device_name or "default", rate=SAMPLE_RATE)

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """PortAudio real-time thread. Copy and leave."""
        if status:
            log.debug("audio_input_status", status=str(status))
        frame = bytes(indata)
        if self.gain != 1.0:
            frame = apply_gain(frame, self.gain)

        with self._lock:
            self._level = rms(frame)
            muted = self._muted
        self._preroll.append(frame)
        if muted:
            return
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # A blocked consumer must not back-pressure the driver.
            self._dropped += 1
            if self._dropped % 50 == 1:
                log.warning("audio_frames_dropped", total=self._dropped)

    async def frames(self) -> Any:
        """Async iterator over captured frames."""
        loop = asyncio.get_running_loop()
        while True:
            frame = await loop.run_in_executor(None, self._blocking_get)
            if frame is None:
                return
            yield frame

    def _blocking_get(self) -> bytes | None:
        try:
            return self._queue.get(timeout=0.5)
        except queue.Empty:
            return b""

    def read_preroll(self) -> bytes:
        """Audio captured just before the current moment."""
        return b"".join(self._preroll)

    def drain(self) -> None:
        """Discard buffered audio — used after speaking, to drop our own voice."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self._muted = muted

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None


class AudioOutput:
    """Blocking playback of synthesised speech, driven from a worker thread."""

    def __init__(self, *, device: str = "", volume: float = 1.0) -> None:
        self.device_name = device
        self.volume = volume
        self._playing = threading.Event()
        self._cancel = threading.Event()

    @property
    def playing(self) -> bool:
        return self._playing.is_set()

    async def play(self, samples: Any, sample_rate: int) -> None:
        """Play a float32 numpy array. Returns when playback finishes or is cancelled."""
        self._cancel.clear()
        await asyncio.to_thread(self._play_sync, samples, sample_rate)

    def _play_sync(self, samples: Any, sample_rate: int) -> None:
        sounddevice = _require_sounddevice()
        device_index = resolve_device(self.device_name, "output")
        self._playing.set()
        try:
            if self.volume != 1.0:
                samples = samples * self.volume
            sounddevice.play(samples, samplerate=sample_rate, device=device_index, blocking=False)
            # Poll so a barge-in can stop playback mid-sentence.
            while sounddevice.get_stream().active:
                if self._cancel.is_set():
                    sounddevice.stop()
                    break
                threading.Event().wait(0.02)
        except Exception as exc:  # noqa: BLE001 - a failed playback is not fatal
            log.warning("playback_failed", error=str(exc))
        finally:
            self._playing.clear()

    def cancel(self) -> None:
        self._cancel.set()


def samples_to_wav_base64(samples: Any, sample_rate: int) -> str | None:
    """Encode a float32 numpy array as a base64 WAV clip.

    For a client that plays audio itself rather than one this process drives
    through a local speaker — the mobile web client, which has no PortAudio
    device to hand a numpy array to and instead needs bytes it can hand to an
    ``<audio>`` element.
    """
    try:
        import numpy
    except ImportError:
        return None
    pcm16 = numpy.clip(numpy.asarray(samples, dtype=numpy.float32), -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(numpy.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def rms(frame: bytes) -> float:
    """Normalised loudness of a 16-bit frame, 0..1."""
    if not frame:
        return 0.0
    try:
        import numpy
    except ImportError:
        return 0.0
    samples = numpy.frombuffer(frame, dtype=numpy.int16).astype(numpy.float32)
    if samples.size == 0:
        return 0.0
    return float(min(1.0, (numpy.sqrt(numpy.mean(samples**2)) / 32768.0) * 4.0))


def apply_gain(frame: bytes, gain: float) -> bytes:
    try:
        import numpy
    except ImportError:
        return frame
    samples = numpy.frombuffer(frame, dtype=numpy.int16).astype(numpy.float32) * gain
    return numpy.clip(samples, -32768, 32767).astype(numpy.int16).tobytes()
