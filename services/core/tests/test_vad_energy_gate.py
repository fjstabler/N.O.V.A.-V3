"""The fallback voice detector, which is not a fallback in practice.

`webrtcvad` is a C extension that needs a compiler to install, so on plenty of
machines — including the Proxmox container this runs on — it is simply absent
and every capture goes through the energy gate below it. That makes this the
detector that decides whether N.O.V.A. hears anything at all, and it has to work
on the audio it actually gets rather than on an idealised recording.

The case it has to get right is the one it was getting wrong: a capture that
starts *in the middle of speech*. That is not an edge case, it is every single
wake-word capture — the microphone opens on the tail of "hey Jarvis" and the
words follow immediately, with no quiet lead-in for a noise gate to calibrate
against.
"""

from __future__ import annotations

import numpy as np
import pytest

from nova.voice.audio import FRAME_SAMPLES
from nova.voice.vad import Endpointer, EndpointState


def frame(level: float) -> bytes:
    """One 80 ms frame of noise at roughly `level` RMS (0.0 to 1.0)."""
    rng = np.random.default_rng(0)
    samples = rng.normal(0, level * 32768, FRAME_SAMPLES)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def gate() -> Endpointer:
    """An endpointer forced onto the energy gate, as a machine without
    webrtcvad gets."""
    detector = Endpointer(silence_ms=800, aggressiveness=2)
    detector._vad = None  # type: ignore[assignment]
    detector.reset()
    return detector


SPEECH = 0.08
QUIET = 0.002


def test_speech_from_the_very_first_frame_is_heard() -> None:
    """The bug, exactly as it happened.

    A wake-word capture begins mid-utterance. Taking the noise floor from the
    first frame anchors it to speech level, and every later frame is then
    compared against three times that and never passes — so the endpointer sat
    in WAITING for the full six seconds and the whole utterance was discarded,
    silently. It looked like "it listens and then does nothing".
    """
    detector = gate()

    for _ in range(10):
        detector.feed(frame(SPEECH))

    assert detector.had_speech, "an utterance that starts immediately was not heard"


def test_a_quiet_room_is_not_mistaken_for_speech() -> None:
    """The other half. A gate that hears everything is as useless as one that
    hears nothing — it would keep the microphone open until the timeout on
    every false wake."""
    detector = gate()

    for _ in range(30):
        detector.feed(frame(QUIET))

    assert not detector.had_speech


def test_speech_after_a_quiet_lead_in_is_heard() -> None:
    """The easy case, which must not regress while fixing the hard one."""
    detector = gate()

    for _ in range(10):
        detector.feed(frame(QUIET))
    for _ in range(10):
        detector.feed(frame(SPEECH))

    assert detector.had_speech


def test_an_utterance_ends_when_the_speaker_stops() -> None:
    """Speech, then silence, then the endpointer says the turn is over."""
    detector = gate()

    for _ in range(10):
        detector.feed(frame(SPEECH))
    result = None
    for _ in range(15):
        result = detector.feed(frame(QUIET))

    assert result is not None and result.state is EndpointState.DONE


def test_a_capture_of_pure_silence_times_out_rather_than_hanging() -> None:
    detector = Endpointer(silence_ms=800, start_timeout_seconds=1.0, aggressiveness=2)
    detector._vad = None  # type: ignore[assignment]
    detector.reset()

    result = None
    for _ in range(20):
        result = detector.feed(frame(QUIET))

    assert result is not None and result.state is EndpointState.TIMEOUT


@pytest.mark.parametrize("level", [0.03, 0.05, 0.1, 0.2])
def test_speech_is_heard_across_the_range_a_microphone_delivers(level: float) -> None:
    """A far-field panel across the room and a phone held to the face differ by
    a lot more than a factor of two."""
    detector = gate()

    for _ in range(10):
        detector.feed(frame(level))

    assert detector.had_speech, f"speech at {level} RMS was not heard"


def test_persistent_background_noise_eventually_stops_counting_as_speech() -> None:
    """The floor still has to adapt, or a noisy room holds the microphone open
    forever. It just must not adapt from a single frame."""
    detector = gate()

    # A television left on: loud, constant, and not addressed to anyone.
    for _ in range(400):
        detector.feed(frame(0.05))

    before = detector.speech_ms
    for _ in range(20):
        detector.feed(frame(0.05))

    assert detector.speech_ms - before < 20 * 80, "the floor never rose to meet the noise"
