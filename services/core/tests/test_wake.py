"""Wake word sensitivity: the setting has to mean what it says.

Regression: settings and the schema document sensitivity as "higher = easier
to trigger", but detection compared the model's confidence score against
`sensitivity` directly — which requires the score to *exceed* it. That makes
a higher sensitivity value a *higher* bar, the opposite of "easier". Turning
sensitivity all the way up was, in practice, the strictest possible setting.
"""

from __future__ import annotations

from typing import Any

from nova.voice.wake import WakeWordDetector


class FakeModel:
    """Stands in for openwakeword's Model — always reports a fixed score."""

    def __init__(self, score: float) -> None:
        self.score = score

    def predict(self, samples: Any) -> dict[str, float]:
        return {"hey_nova": self.score}


def make_detector(sensitivity: float, score: float) -> WakeWordDetector:
    # consecutive_frames pinned here, not left at the production default —
    # this file is about what sensitivity does, and fire_twice() below
    # should not silently need updating whenever that default changes.
    detector = WakeWordDetector("hey_nova.onnx", sensitivity=sensitivity, consecutive_frames=2)
    detector._model = FakeModel(score)  # type: ignore[assignment]
    return detector


def fire_twice(detector: WakeWordDetector) -> bool:
    """Two consecutive qualifying frames are required before a hit fires."""
    frame = b"\x00\x00" * 80
    detector.detected(frame)
    return detector.detected(frame)


def test_high_sensitivity_accepts_a_middling_score() -> None:
    """Comparing the score against `sensitivity` directly made the *maximum*
    setting the strictest one — this middling 0.4 confidence score would
    have been rejected at sensitivity=0.9 under the old, inverted logic."""
    assert fire_twice(make_detector(sensitivity=0.9, score=0.4)) is True


def test_low_sensitivity_rejects_the_same_middling_score() -> None:
    assert fire_twice(make_detector(sensitivity=0.1, score=0.4)) is False


def test_a_near_certain_score_passes_at_either_extreme() -> None:
    assert fire_twice(make_detector(sensitivity=0.9, score=0.95)) is True
    assert fire_twice(make_detector(sensitivity=0.1, score=0.95)) is True


def test_a_very_weak_score_fails_at_either_extreme() -> None:
    assert fire_twice(make_detector(sensitivity=0.9, score=0.02)) is False
    assert fire_twice(make_detector(sensitivity=0.1, score=0.02)) is False


def test_the_shipped_default_sits_slightly_lenient_of_the_midpoint() -> None:
    """sensitivity=0.55 (the schema default) should accept a just-above-half
    score and reject a just-below-half one."""
    assert fire_twice(make_detector(sensitivity=0.55, score=0.46)) is True
    assert fire_twice(make_detector(sensitivity=0.55, score=0.44)) is False


# ---------------------------------------------------------- consecutive frames


def test_consecutive_frames_rejects_a_brief_high_confidence_spike() -> None:
    """Regression: a real false-wake report showed the model scoring 0.98+
    confidence — high enough that no sensitivity setting would ever reject
    it — but only briefly, consistent with a word or two of TV/ambient audio
    that happens to resemble the phrase rather than someone actually saying
    it. Sensitivity only filters borderline scores; a longer required streak
    is the lever that actually catches this, which is the point of the
    setting existing at all."""
    detector = WakeWordDetector(
        "hey_nova.onnx", sensitivity=0.55, consecutive_frames=4, models_dir=None
    )
    detector._model = FakeModel(0.99)  # type: ignore[assignment]
    frame = b"\x00\x00" * 80

    # Three consecutive high-confidence frames — not enough at consecutive_frames=4.
    assert detector.detected(frame) is False
    assert detector.detected(frame) is False
    assert detector.detected(frame) is False
    assert detector.detected(frame) is True


def test_a_score_dip_mid_streak_resets_the_count() -> None:
    detector = WakeWordDetector(
        "hey_nova.onnx", sensitivity=0.55, consecutive_frames=3, models_dir=None
    )
    frame = b"\x00\x00" * 80

    detector._model = FakeModel(0.9)  # type: ignore[assignment]
    assert detector.detected(frame) is False
    assert detector.detected(frame) is False

    detector._model = FakeModel(0.1)  # type: ignore[assignment]  # dips below threshold
    assert detector.detected(frame) is False

    detector._model = FakeModel(0.9)  # type: ignore[assignment]
    assert detector.detected(frame) is False  # streak restarted, not resumed
    assert detector.detected(frame) is False
    assert detector.detected(frame) is True


def test_the_shipped_default_is_three_consecutive_frames() -> None:
    """Regression: real logs showed repeated wake_detected events during a
    long TV-watching session, several with 0.98+ confidence — the old
    default of 2 consecutive frames (~160ms) was not enough debounce
    against ambient audio, even though sensitivity had already been
    lowered. 3 is the new floor; still comfortably short of how long an
    actually-spoken "Hey Nova" holds a high score for."""
    detector = WakeWordDetector("hey_nova.onnx", sensitivity=0.55, models_dir=None)
    assert detector.consecutive_frames == 3
