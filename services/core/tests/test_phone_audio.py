"""Turning a telephone into something a speech model can hear, and back.

This is the layer with no forgiving failure mode. Every other part of a phone
call degrades gracefully when it is slightly wrong; this one turns speech into
noise. The first draft of the encoder here looked entirely plausible, matched
its reference on half the sample range, and encoded silence as a quarter of
full scale — which no amount of reading it back would have shown.

So the tests measure rather than inspect: fixed vectors from the reference
implementation, and a signal-to-noise figure on a round trip.
"""

from __future__ import annotations

import numpy as np
import pytest

from nova.phone.audio import (
    MODEL_RATE,
    PHONE_RATE,
    Downsampler,
    Upsampler,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)

#: `(sample, μ-law code, what that code decodes back to)`, taken from the
#: standard library's `audioop` while it still exists. Hardcoded rather than
#: computed at test time on purpose: `audioop` was removed in Python 3.13, and
#: a test that disappears with it would take the only real check with it.
GOLDEN = [
    (0, 0xFF, 0),
    (1, 0xFF, 0),
    (-1, 0x7E, -8),
    (100, 0xF2, 104),
    (-100, 0x72, -104),
    (1000, 0xCE, 988),
    (-1000, 0x4E, -988),
    (8000, 0xA0, 7932),
    (-8000, 0x20, -7932),
    (32767, 0x80, 32124),
    (-32768, 0x00, -32124),
    (16384, 0x8F, 16764),
    (-16384, 0x0F, -16764),
    (132, 0xEF, 132),
    (-132, 0x6F, -132),
]


# --------------------------------------------------------------------- μ-law


@pytest.mark.parametrize("sample,code,_decoded", GOLDEN)
def test_encoding_matches_the_reference(sample: int, code: int, _decoded: int) -> None:
    assert pcm16_to_mulaw(np.array([sample], dtype=np.int16)) == bytes([code])


@pytest.mark.parametrize("_sample,code,decoded", GOLDEN)
def test_decoding_matches_the_reference(_sample: int, code: int, decoded: int) -> None:
    assert mulaw_to_pcm16(bytes([code]))[0] == decoded


def test_silence_encodes_to_silence() -> None:
    """The single most diagnostic case. The broken first draft encoded zero as
    a quarter of full scale, which on a call is a loud tone down the line
    before anybody says anything."""
    assert pcm16_to_mulaw(np.zeros(8, dtype=np.int16)) == b"\xff" * 8
    assert not mulaw_to_pcm16(b"\xff" * 8).any()


def test_a_round_trip_keeps_the_signal_well_above_the_noise() -> None:
    """μ-law is lossy by design, so the question is not whether it changes the
    samples but by how much. Around 37 dB is what the encoding is worth; the
    broken draft managed -3, which is noise louder than the signal."""
    rng = np.random.default_rng(0)
    speech = (rng.standard_normal(50_000) * 6000).clip(-32768, 32767).astype(np.int16)

    recovered = mulaw_to_pcm16(pcm16_to_mulaw(speech)).astype(float)
    original = speech.astype(float)
    snr = 10 * np.log10((original**2).sum() / ((recovered - original) ** 2).sum())

    assert snr > 30, f"only {snr:.1f} dB of signal survived the round trip"


def test_the_sign_survives() -> None:
    """A sign convention inverted here would put every waveform upside down —
    which is inaudible on its own and catastrophic once mixed with anything."""
    loud = np.array([20000, -20000], dtype=np.int16)

    recovered = mulaw_to_pcm16(pcm16_to_mulaw(loud))

    assert recovered[0] > 0 and recovered[1] < 0


def test_empty_input_is_not_an_error() -> None:
    """A call has gaps in it."""
    assert pcm16_to_mulaw(np.zeros(0, dtype=np.int16)) == b""
    assert mulaw_to_pcm16(b"").size == 0


# ---------------------------------------------------------------- resampling


def tone(frequency: float, rate: int, seconds: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * frequency * t) * 12000).astype(np.int16)


def test_downsampling_halves_the_rate() -> None:
    out = Downsampler().process(tone(440, MODEL_RATE, 1.0))

    assert abs(out.size - PHONE_RATE) <= 1


def test_upsampling_doubles_the_rate() -> None:
    out = Upsampler().process(tone(440, PHONE_RATE, 1.0))

    assert out.size == MODEL_RATE


def test_a_voice_frequency_survives_the_journey_out_and_back() -> None:
    """440 Hz sits in the middle of the telephone band, so it should come back
    recognisably — same frequency, similar amplitude."""
    original = tone(440, MODEL_RATE)

    narrow = Downsampler().process(original)
    wide = Upsampler().process(narrow)

    assert wide.size == original.size
    # Amplitude within a few percent, and the peak of the spectrum in the same place.
    assert 0.85 < wide.astype(float).std() / original.astype(float).std() < 1.15
    peak = np.argmax(np.abs(np.fft.rfft(wide.astype(float))))
    assert abs(peak - np.argmax(np.abs(np.fft.rfft(original.astype(float))))) <= 1


def attenuation_db(frequency: float) -> float:
    """How much of a tone at `frequency` survives the trip down to 8 kHz."""
    original = tone(frequency, MODEL_RATE)
    out = Downsampler().process(original)
    return 20 * np.log10(max(out.astype(float).std(), 1e-9) / original.astype(float).std())


@pytest.mark.parametrize("frequency", [300, 1000, 3000])
def test_the_speech_band_passes_through_untouched(frequency: float) -> None:
    assert attenuation_db(frequency) > -1.0


@pytest.mark.parametrize("frequency", [4000, 5000, 6000, 7000])
def test_anything_that_would_alias_is_removed_first(frequency: float) -> None:
    """The reason the downsampler filters at all.

    A 6 kHz tone cannot fit in an 8 kHz stream. Decimated without filtering it
    does not vanish — it comes back as 2 kHz, sitting under the voice as a
    second tone that was never spoken. Measured as a level rather than by
    looking for the fold: with a pure tone going in, the alias is the only
    thing in the output, so it is the loudest thing there however quiet it is.
    """
    assert attenuation_db(frequency) < -35.0


@pytest.mark.parametrize("resampler,rate", [(Downsampler, MODEL_RATE), (Upsampler, PHONE_RATE)])
def test_processing_in_packets_gives_the_same_answer_as_all_at_once(
    resampler: type, rate: int
) -> None:
    """The reason both resamplers hold state.

    Audio arrives in 20 ms packets. Filtering each one from a standing start
    puts a step at every join — fifty a second, heard as a buzz under the
    voice rather than as anything identifiable. Carrying the tail across makes
    the packet stream identical to the whole signal, which is the only
    definition of "no boundary artefact" worth testing.
    """
    signal = tone(440, rate, 0.5)
    packet = rate // 50  # 20 ms, which is what a phone line delivers

    whole = resampler().process(signal)

    streaming = resampler()
    pieces = [streaming.process(signal[i : i + packet]) for i in range(0, signal.size, packet)]
    joined = np.concatenate(pieces)

    assert joined.size == whole.size
    assert np.array_equal(joined, whole)


def test_a_resampler_can_be_reused_for_the_next_call() -> None:
    """State that survives a hang-up would leak the tail of one call into the
    beginning of the next."""
    down = Downsampler()
    down.process(tone(440, MODEL_RATE))
    down.reset()

    assert np.array_equal(
        down.process(tone(440, MODEL_RATE)), Downsampler().process(tone(440, MODEL_RATE))
    )
