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
    detector = WakeWordDetector("hey_nova.onnx", sensitivity=sensitivity)
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
