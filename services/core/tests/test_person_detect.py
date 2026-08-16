"""On-device person detection: 'is anyone in the room', reliably.

The model call itself needs PyTorch and weights, so it's isolated behind
``PersonDetector.detect``; everything tested here is the pure parsing and phrasing
plus the skill wiring, with the detector and camera faked. No torch, no webcam.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from nova.context import NovaContext
from nova.integrations.person_detect import describe_people, person_confidences
from nova.runtime.errors import MissingDependency, SkillError
from nova.skills.builtin import camera as camera_module
from nova.skills.builtin.camera import CameraSkill

# ------------------------------------------------------------------ parsing


def _result(confidences: list[float]) -> Any:
    """Mimic an Ultralytics result: ``result.boxes.conf.tolist()``."""
    conf = SimpleNamespace(tolist=lambda: confidences)
    return SimpleNamespace(boxes=SimpleNamespace(conf=conf))


def test_person_confidences_reads_every_box() -> None:
    results = [_result([0.9, 0.55])]
    assert person_confidences(results) == [0.9, 0.55]


def test_person_confidences_tolerates_an_empty_result() -> None:
    assert person_confidences([SimpleNamespace(boxes=None)]) == []


def test_describe_people_phrasing() -> None:
    assert "empty" in describe_people([]).lower()
    assert describe_people([0.9]).startswith("Yes") and "1 person" in describe_people([0.9])
    assert "2 people" in describe_people([0.9, 0.8])


# -------------------------------------------------------------------- skill


class FakePersonDetector:
    def __init__(self, confidences: list[float]) -> None:
        self.confidence = 0.4
        self._confidences = confidences
        self.frames_seen = 0

    def detect(self, frame: Any) -> list[float]:
        self.frames_seen += 1
        return self._confidences


def make_skill(ctx: NovaContext, detector: Any) -> CameraSkill:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    skill = CameraSkill(ctx)
    skill._person_detector = detector  # type: ignore[attr-defined]
    return skill


async def test_look_for_people_reports_a_person(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    detector = FakePersonDetector([0.88])
    skill = make_skill(ctx, detector)

    async def fake_read(index: int) -> Any:
        return np.zeros((8, 8, 3), dtype=np.uint8)

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)

    result = await skill.look_for_people()

    assert "Yes" in result and "1 person" in result
    assert detector.frames_seen == 1


async def test_look_for_people_reports_an_empty_room(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = make_skill(ctx, FakePersonDetector([]))

    async def fake_read(index: int) -> Any:
        return np.zeros((8, 8, 3), dtype=np.uint8)

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)

    result = await skill.look_for_people()
    assert "empty" in result.lower()


async def test_look_for_people_applies_the_configured_confidence(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    detector = FakePersonDetector([0.5])
    skill = make_skill(ctx, detector)
    ctx.store.patch({"vision": {"person_confidence": 0.7}}, persist=False)

    async def fake_read(index: int) -> Any:
        return np.zeros((8, 8, 3), dtype=np.uint8)

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)

    await skill.look_for_people()
    assert detector.confidence == 0.7  # pushed from settings before detecting


async def test_look_for_people_degrades_without_the_extra(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MissingDetector:
        confidence = 0.4

        def detect(self, frame: Any) -> list[float]:
            raise MissingDependency("person detection", "ultralytics", "person")

    skill = make_skill(ctx, MissingDetector())

    async def fake_read(index: int) -> Any:
        return np.zeros((8, 8, 3), dtype=np.uint8)

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)

    with pytest.raises(SkillError, match="ultralytics"):
        await skill.look_for_people()
