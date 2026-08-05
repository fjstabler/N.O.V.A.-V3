"""samples_to_wav_base64: the mobile client's own reply audio.

The mobile web client cannot use PortAudio, so a reply meant for it is
encoded as a WAV clip it can hand straight to an <audio> element instead of
being played out the core's local speaker. This checks the encoding round
-trips to real, correctly-shaped PCM rather than just "some bytes".
"""

from __future__ import annotations

import base64
import wave
from io import BytesIO

import numpy

from nova.voice.audio import samples_to_wav_base64


def decode(encoded: str) -> wave.Wave_read:
    return wave.open(BytesIO(base64.b64decode(encoded)), "rb")


def test_round_trips_silence_to_correct_shape() -> None:
    samples = numpy.zeros(2400, dtype=numpy.float32)
    encoded = samples_to_wav_base64(samples, 24000)
    assert encoded is not None

    with decode(encoded) as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 24000
        assert wav_file.getnframes() == 2400
        pcm = numpy.frombuffer(wav_file.readframes(2400), dtype=numpy.int16)
        assert numpy.all(pcm == 0)


def test_full_scale_samples_hit_int16_extremes_without_wrapping() -> None:
    samples = numpy.array([1.0, -1.0, 0.5], dtype=numpy.float32)
    encoded = samples_to_wav_base64(samples, 16000)
    assert encoded is not None

    with decode(encoded) as wav_file:
        pcm = numpy.frombuffer(wav_file.readframes(3), dtype=numpy.int16)
        assert pcm[0] == 32767
        assert pcm[1] == -32767
        assert 16000 < pcm[2] < 16800


def test_out_of_range_samples_are_clipped_not_wrapped() -> None:
    samples = numpy.array([2.0, -3.0], dtype=numpy.float32)
    encoded = samples_to_wav_base64(samples, 16000)
    assert encoded is not None

    with decode(encoded) as wav_file:
        pcm = numpy.frombuffer(wav_file.readframes(2), dtype=numpy.int16)
        assert pcm[0] == 32767
        assert pcm[1] == -32767
