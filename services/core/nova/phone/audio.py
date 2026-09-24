"""Between a telephone and a speech model.

Two conversions, both ways, and neither is optional: a phone line carries 8 kHz
G.711 μ-law, and Whisper and Kokoro both want 16 kHz linear PCM. Get either one
wrong and the result is not a subtle degradation — it is noise, or silence, or
a voice an octave out.

Written on numpy rather than the standard library's `audioop`, which does
exactly this and was removed in Python 3.13. Depending on it would mean the
telephone stops working the next time somebody upgrades Python, in a way whose
cause is nowhere near its symptom.

**The resamplers are stateful and have to be.** Audio arrives in 20 ms packets.
A filter applied to each packet in isolation starts from silence every time,
which puts a discontinuity at every boundary — fifty clicks a second, heard as
a buzz sitting under the voice. Each direction therefore carries the tail of
the previous packet across the join.
"""

from __future__ import annotations

import numpy as np

#: What a phone line speaks.
PHONE_RATE = 8000
#: What the speech models speak.
MODEL_RATE = 16000

#: G.711 μ-law constants, from the ITU-T recommendation. The decoder works in
#: 16-bit units; the encoder shifts to 14 bits first, so it needs its own
#: scaled pair rather than reusing these.
_MU_BIAS = 0x84
_MU_BIAS_14 = _MU_BIAS >> 2
_MU_CLIP_14 = 8159


def _build_decode_table() -> np.ndarray:
    """The 256 signal levels a μ-law byte can mean."""
    codes = np.arange(256, dtype=np.int32)
    # The byte arrives with every bit inverted.
    inverted = ~codes & 0xFF
    sign = inverted & 0x80
    exponent = (inverted >> 4) & 0x07
    mantissa = inverted & 0x0F

    magnitude = ((mantissa << 3) + _MU_BIAS) << exponent
    magnitude -= _MU_BIAS
    values = np.where(sign, -magnitude, magnitude)
    return values.astype(np.int16)


#: Segment upper bounds, in 14-bit units. The encoder's segment is simply the
#: first of these the biased magnitude fits under.
_SEGMENT_ENDS = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF], dtype=np.int32)


def _build_encode_table() -> np.ndarray:
    """Every 16-bit sample's μ-law byte, worked out once.

    65 536 entries is 64 KB and turns the encoder into an array index, which
    matters: this runs on every outgoing packet for the length of a call.

    This follows the reference implementation exactly rather than approximately.
    Three details in it are not guessable and all three are fatal if missed: the
    input is shifted to 14 bits *first* and the bias scaled to match, the
    mantissa is taken from `segment + 1` rather than the segment, and the sign
    is applied by exclusive-or with a mask (0xFF positive, 0x7F negative), which
    is not the same as inverting the whole byte.

    A version of this assembled from memory looked entirely reasonable and
    encoded silence as a quarter of full scale.
    """
    samples = np.arange(-32768, 32768, dtype=np.int32)

    # 16-bit to 14-bit, floor-shifted, before anything else touches it.
    values = samples >> 2
    mask = np.where(values < 0, 0x7F, 0xFF).astype(np.int32)
    magnitude = np.minimum(np.abs(values), _MU_CLIP_14) + _MU_BIAS_14

    segment = np.searchsorted(_SEGMENT_ENDS, magnitude, side="left").astype(np.int32)
    # Only reachable at the clipping point, where the bias pushes the magnitude
    # one past the last segment's ceiling.
    overflow = segment >= len(_SEGMENT_ENDS)
    safe = np.clip(segment, 0, len(_SEGMENT_ENDS) - 1)

    mantissa = (magnitude >> (safe + 1)) & 0x0F
    encoded = ((safe << 4) | mantissa) ^ mask
    encoded = np.where(overflow, 0x7F ^ mask, encoded)
    return (encoded & 0xFF).astype(np.uint8)


_DECODE = _build_decode_table()
_ENCODE = _build_encode_table()


def mulaw_to_pcm16(payload: bytes) -> np.ndarray:
    """One μ-law packet as int16 samples."""
    if not payload:
        return np.zeros(0, dtype=np.int16)
    return _DECODE[np.frombuffer(payload, dtype=np.uint8)]


def pcm16_to_mulaw(samples: np.ndarray) -> bytes:
    """int16 samples as a μ-law packet."""
    if samples.size == 0:
        return b""
    clipped = np.clip(samples, -32768, 32767).astype(np.int32)
    return _ENCODE[clipped + 32768].tobytes()


# ------------------------------------------------------------------ filtering


def _lowpass(cutoff: float, rate: int, taps: int = 63) -> np.ndarray:
    """A windowed-sinc low-pass, normalised to unity gain at DC.

    Odd tap count so the delay is a whole number of samples, which keeps the
    filter from shifting the audio by half a sample and makes the tail
    bookkeeping below exact rather than approximate.
    """
    if taps % 2 == 0:  # pragma: no cover - guarded against a careless caller
        taps += 1
    n = np.arange(taps) - (taps - 1) / 2
    kernel = np.sinc(2 * cutoff / rate * n) * np.hamming(taps)
    return (kernel / kernel.sum()).astype(np.float32)


#: 3.4 kHz is the top of the telephone band. Anything above it cannot survive
#: the trip anyway, and removing it before decimating is what stops it folding
#: back down as aliasing — which does not sound like missing treble, it sounds
#: like a second, wrong voice underneath.
_ANTI_ALIAS = _lowpass(3400.0, MODEL_RATE)


class Downsampler:
    """16 kHz PCM to 8 kHz, for speech on its way out to the line."""

    def __init__(self) -> None:
        self._taps = _ANTI_ALIAS
        self._tail = np.zeros(len(self._taps) - 1, dtype=np.float32)

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return np.zeros(0, dtype=np.int16)

        block = np.concatenate([self._tail, samples.astype(np.float32)])
        # Keep the last (taps - 1) input samples so the next block's filter
        # starts where this one left off instead of from silence.
        self._tail = block[-(len(self._taps) - 1) :]

        filtered = np.convolve(block, self._taps, mode="valid")
        return np.clip(filtered[::2], -32768, 32767).astype(np.int16)

    def reset(self) -> None:
        self._tail[:] = 0.0


class Upsampler:
    """8 kHz PCM from the line to 16 kHz, for the transcriber.

    Linear interpolation rather than a filtered insert. The source genuinely
    holds nothing above 4 kHz — the line put it there — so the images an
    interpolator leaves sit in a band that was empty to begin with, and Whisper
    is entirely untroubled by them.
    """

    def __init__(self) -> None:
        self._previous = np.float32(0.0)

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return np.zeros(0, dtype=np.int16)

        current = samples.astype(np.float32)
        # The midpoints, each between one input sample and the next. The first
        # is between this block and the last one — which is the whole reason
        # the previous sample is kept.
        previous = np.concatenate([[self._previous], current[:-1]])
        midpoints = (previous + current) / 2.0
        self._previous = current[-1]

        interleaved = np.empty(current.size * 2, dtype=np.float32)
        interleaved[0::2] = midpoints
        interleaved[1::2] = current
        return np.clip(interleaved, -32768, 32767).astype(np.int16)

    def reset(self) -> None:
        self._previous = np.float32(0.0)
