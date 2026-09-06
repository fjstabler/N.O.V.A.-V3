"""A microphone and a speaker that live on another device.

The voice pipeline is built around one continuous 16 kHz mono stream and a
speaker it can block on. Both come from PortAudio when N.O.V.A. runs on a
machine with sound hardware — which is the one thing a headless container in a
rack does not have, and the one thing a wall panel has plenty of.

This module supplies the same two ends over the bridge instead. A client sends
microphone frames up and plays synthesised speech back down; every stage in
between stays exactly where it was. The wake phrase and its sensitivity, the
endpointer's silence window, the Whisper model, the Kokoro voice and the whole
follow-up conversation window remain the core's. The remote device contributes
hardware, not behaviour — which is why a panel in the kitchen and a headset at
the desk hear the same assistant, with one set of settings behind them.

Two details are worth knowing before changing anything here:

*Re-chunking is mandatory.* openWakeWord scores exactly 1280 samples at a time
and a network is free to deliver bytes in whatever sizes it likes. Frames are
buffered and re-cut to :data:`FRAME_BYTES` before any consumer sees them, so a
client that batches four frames into one message — or splits one across two —
still produces a stream the detector can score.

*The clock is the core's, not the client's.* Playback is timed from the sample
count rather than waiting for the device to report back. A panel that drops off
mid-sentence therefore cannot strand the state machine in SPEAKING; the worst
case is that N.O.V.A. believes it finished a sentence nobody heard.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..runtime.logging import get_logger
from .audio import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE, apply_gain, rms, samples_to_wav_base64

log = get_logger(__name__)

#: Emitted when the core wants the attached device to play a clip.
REMOTE_PLAY = "voice.remote.play"
#: Emitted to stop playback early — a barge-in, or an explicit cancel.
REMOTE_STOP = "voice.remote.stop"
#: Emitted to tell the device whether to keep sending microphone frames.
REMOTE_CAPTURE = "voice.remote.capture"

#: Playback is timed from the sample count; a little slack absorbs the network
#: hop and the device's own buffer so the follow-up window does not open while
#: the last syllable is still in the air.
PLAYBACK_PAD_SECONDS = 0.35


class RemoteMicrophone:
    """A microphone attached to another device.

    Mirrors :class:`~nova.voice.audio.AudioInput` closely enough to stand in
    for it, including the pre-roll ring buffer that keeps the first word after
    the wake phrase from being clipped. The differences are all consequences of
    the frames arriving on the event loop instead of a real-time thread: an
    ``asyncio.Queue`` rather than a threading one, and no lock, because every
    mutation already happens on the loop.
    """

    #: Seconds of audio retained from before the wake word fired.
    PREROLL_SECONDS = 1.0
    #: Frames held before the oldest are dropped. At 80 ms each this is ~5 s —
    #: deep enough to ride out a hiccup, shallow enough that a client which
    #: falls behind is heard late rather than heard out of order.
    QUEUE_FRAMES = 64
    #: A source that has sent nothing for this long has gone away, whether or
    #: not it managed to say so.
    STALE_SECONDS = 10.0

    def __init__(self, *, gain: float = 1.0) -> None:
        self.gain = gain
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self.QUEUE_FRAMES)
        self._preroll: deque[bytes] = deque(
            maxlen=int(self.PREROLL_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
        )
        self._pending = bytearray()
        self._level = 0.0
        self._muted = False
        self._closed = False
        self._dropped = 0
        self._frames_in = 0
        self._last_frame_at = time.monotonic()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Nothing to open — the device on the other end already did that."""
        self._closed = False
        self._last_frame_at = time.monotonic()
        log.info("remote_microphone_attached", rate=SAMPLE_RATE)

    def stop(self) -> None:
        self._closed = True
        self._pending.clear()
        # Wake a consumer parked on the queue so `frames()` can return.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(b"")
        log.info("remote_microphone_detached", frames=self._frames_in, dropped=self._dropped)

    # ----------------------------------------------------------------- input

    def submit(self, pcm: bytes) -> int:
        """Accept PCM from the device; returns how many whole frames it made.

        Called once per message off the bridge. Whatever size the client sent
        is re-cut to the frame the wake detector is trained on, so batching or
        splitting on the wire changes throughput and never correctness.
        """
        if self._closed or not pcm:
            return 0
        self._last_frame_at = time.monotonic()
        self._pending.extend(pcm)

        produced = 0
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            if self.gain != 1.0:
                frame = apply_gain(frame, self.gain)
            self._level = rms(frame)
            self._preroll.append(frame)
            produced += 1
            self._frames_in += 1
            if self._muted:
                continue
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Match the local microphone: drop, never back-pressure the
                # producer. Here the producer is a socket, and stalling it
                # would queue audio in the kernel instead of admitting loss.
                self._dropped += 1
                if self._dropped % 50 == 1:
                    log.warning("remote_frames_dropped", total=self._dropped)
        return produced

    async def frames(self) -> Any:
        """Async iterator over received frames."""
        while not self._closed:
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                yield b""  # idle tick, same as a silent local device
                continue
            if self._closed:
                return
            yield frame

    # ------------------------------------------------------------- consumers

    def read_preroll(self) -> bytes:
        return b"".join(self._preroll)

    def drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    @property
    def level(self) -> float:
        return self._level

    @property
    def stale(self) -> bool:
        return time.monotonic() - self._last_frame_at > self.STALE_SECONDS

    @property
    def seconds_since_frame(self) -> float:
        return time.monotonic() - self._last_frame_at


class RemoteSpeaker:
    """Playback on another device.

    Stands in for :class:`~nova.voice.audio.AudioOutput`. Where that one hands
    samples to PortAudio and polls until the stream drains, this encodes them
    once, publishes them, and waits out the clip's own duration — so
    ``SPEAKING`` lasts as long on a panel as it does on a desktop, and a
    barge-in still lands mid-sentence.
    """

    def __init__(
        self, publish: Callable[[str, dict[str, Any]], None], *, volume: float = 1.0
    ) -> None:
        self._publish = publish
        self.volume = volume
        self._cancel = asyncio.Event()
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    async def play(self, samples: Any, sample_rate: int) -> None:
        self._cancel.clear()
        encoded = samples_to_wav_base64(_scaled(samples, self.volume), sample_rate)
        if encoded is None:
            log.warning("remote_playback_encode_failed")
            return

        duration = _duration_seconds(samples, sample_rate)
        self._playing = True
        self._publish(
            REMOTE_PLAY,
            {"wav": encoded, "sampleRate": sample_rate, "durationMs": round(duration * 1000)},
        )
        try:
            # Returning early would open the follow-up window over N.O.V.A.'s
            # own voice; waiting on the device to confirm would let a panel
            # that dropped off wedge the state machine. The clip's own length
            # is the one clock that cannot go missing.
            await asyncio.wait_for(self._cancel.wait(), timeout=duration + PLAYBACK_PAD_SECONDS)
        except TimeoutError:
            pass  # played to the end, the ordinary case
        else:
            self._publish(REMOTE_STOP, {})
        finally:
            self._playing = False

    def cancel(self) -> None:
        self._cancel.set()


def _scaled(samples: Any, volume: float) -> Any:
    if volume == 1.0:
        return samples
    try:
        return samples * volume
    except TypeError:  # pragma: no cover - a list rather than an ndarray
        return [s * volume for s in samples]


def _duration_seconds(samples: Any, sample_rate: int) -> float:
    try:
        count = len(samples)
    except TypeError:
        return 0.0
    return count / float(sample_rate) if sample_rate > 0 else 0.0
