"""On-device camera detection: motion differencing and the face count.

The motion maths is pure NumPy, so it is tested directly with synthetic frames —
no camera, no OpenCV. The skill tests fake the camera pool and (for the face
count) borrow a stand-in security engine, so the whole surface runs without a
webcam or the vision model.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from nova.context import NovaContext
from nova.integrations.detection import MotionDetector, looks_blank, mean_brightness
from nova.runtime.errors import SkillError
from nova.runtime.service import Service, ServiceState
from nova.skills.builtin import camera as camera_module
from nova.skills.builtin.camera import CameraSkill

# ------------------------------------------------------------- motion maths


def frame(value: int = 0, size: int = 20) -> Any:
    return np.full((size, size, 3), value, dtype=np.uint8)


def test_identical_frames_show_no_motion() -> None:
    detector = MotionDetector()
    result = detector.detect(frame(0), frame(0))
    assert result.moved is False
    assert result.score == 0.0


def test_a_large_change_is_motion() -> None:
    detector = MotionDetector()
    a = frame(0)
    b = frame(0)
    b[:10, :, :] = 255  # half the frame lights up
    result = detector.detect(a, b)
    assert result.moved is True
    assert result.score == pytest.approx(0.5)


def test_a_tiny_change_stays_below_the_threshold() -> None:
    detector = MotionDetector(min_area_fraction=0.02)
    a = frame(0)
    b = frame(0)
    b[0, 0, :] = 255  # 1 pixel of 400 → 0.0025
    result = detector.detect(a, b)
    assert result.moved is False


def test_sensor_noise_below_the_pixel_delta_is_ignored() -> None:
    detector = MotionDetector(pixel_delta=25)
    a = frame(0)
    b = frame(10)  # every pixel shifts by 10, under the delta
    assert detector.score(a, b) == 0.0


def test_mismatched_shapes_read_as_still() -> None:
    detector = MotionDetector()
    assert detector.score(frame(0, size=20), frame(0, size=10)) == 0.0


def test_greyscale_frames_are_handled() -> None:
    detector = MotionDetector()
    a = np.zeros((10, 10), dtype=np.uint8)
    b = np.zeros((10, 10), dtype=np.uint8)
    b[:5, :] = 255
    assert detector.detect(a, b).moved is True


# ------------------------------------------------------------- blank frames


def test_looks_blank_flags_a_black_frame() -> None:
    assert looks_blank(frame(0)) is True  # a black frame = wrong index / covered lens
    assert looks_blank(frame(200)) is False


def test_mean_brightness() -> None:
    assert mean_brightness(frame(255)) == 255.0
    assert mean_brightness(frame(0)) == 0.0


async def test_check_for_motion_flags_a_dead_camera(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two black frames is a broken camera, not 'no movement' — surface it."""
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)

    async def fake_read(index: int) -> Any:
        return frame(0)  # always black

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)
    monkeypatch.setattr(camera_module.asyncio, "sleep", no_sleep)

    with pytest.raises(SkillError, match="black image"):
        await CameraSkill(ctx).check_for_motion(camera_index=0)


# ------------------------------------------------------------------- skill


async def test_skill_is_unavailable_until_the_camera_is_enabled(ctx: NovaContext) -> None:
    available, reason = CameraSkill(ctx).is_available()
    assert available is False and "disabled" in reason

    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    assert CameraSkill(ctx).is_available()[0] is True


async def test_check_for_motion_compares_two_frames(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    still = frame(50)  # a live (non-black) frame
    moved = frame(50)
    moved[:12, :, :] = 255  # a big region lights up
    calls = {"n": 0}

    async def fake_read(index: int) -> Any:
        calls["n"] += 1
        return still if calls["n"] == 1 else moved

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)

    result = await CameraSkill(ctx).check_for_motion(camera_index=0, seconds=1.0)

    assert "Motion detected" in result
    assert calls["n"] > 2  # reads continuously across the window, not just twice


# --------------------------------------------------------------- face count


class _Obs:
    def __init__(self, embedding: Any) -> None:
        self.embedding = embedding


class FakeEngine:
    def __init__(self, observations: list[_Obs]) -> None:
        self.loaded = True
        self._observations = observations

    def observe(self, _frame: Any) -> list[_Obs]:
        return self._observations


class FakeFaces:
    def __init__(self, enrolled: list[str], matches: dict[Any, str]) -> None:
        self._enrolled = enrolled
        self._matches = matches

    def names(self) -> list[str]:
        return self._enrolled

    def match(self, embedding: Any, *, threshold: float) -> tuple[str, float] | None:
        name = self._matches.get(embedding)
        return (name, 0.9) if name is not None else None


class FakeSecurity(Service):
    name = "security"

    def __init__(self, ctx: NovaContext, engine: FakeEngine, faces: FakeFaces) -> None:
        super().__init__(ctx)
        self.engine = engine
        self.faces = faces


def install_security(ctx: NovaContext, engine: FakeEngine, faces: FakeFaces) -> None:
    service = FakeSecurity(ctx, engine, faces)
    ctx.services.register(service)
    service._set_state(ServiceState.RUNNING)


async def test_count_people_needs_the_security_service(ctx: NovaContext) -> None:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    with pytest.raises(SkillError, match="security service"):
        await CameraSkill(ctx).count_people()


async def test_count_people_counts_and_names_the_recognised(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    observations = [_Obs("fin"), _Obs("stranger")]
    install_security(
        ctx, FakeEngine(observations), FakeFaces(enrolled=["Fin"], matches={"fin": "Fin"})
    )

    async def fake_read(index: int) -> Any:
        return frame(0)

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)

    result = await CameraSkill(ctx).count_people()

    assert "2 people" in result
    assert "I recognise Fin" in result


async def test_count_people_reports_an_empty_room(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"vision": {"camera_enabled": True}}, persist=False)
    install_security(ctx, FakeEngine([]), FakeFaces(enrolled=[], matches={}))

    async def fake_read(index: int) -> Any:
        return frame(0)

    monkeypatch.setattr(camera_module.local_camera_pool, "read_bgr", fake_read)

    result = await CameraSkill(ctx).count_people()

    assert "can't see anyone" in result
