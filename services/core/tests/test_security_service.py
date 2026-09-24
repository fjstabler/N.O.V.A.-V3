"""SecurityService: arming, the watch loop's decisions, and the alert pipeline.

The camera and the face engine are both faked here — what is under test is
the decision logic around them (confirm-frame debounce, alert cooldown,
which channels fire and in what shape), not OpenCV itself.
"""

from __future__ import annotations

from typing import Any

import pytest

import nova.security.service as security_service_module
from nova.context import NovaContext
from nova.integrations.local_camera import local_camera_pool
from nova.notifications import Notification
from nova.runtime.errors import SkillError
from nova.security.faces import FaceObservation
from nova.security.service import SecurityService


class FakeVoice:
    name = "voice"
    running = True

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, text: str) -> None:
        self.spoken.append(text)


class FakeNotifications:
    name = "notifications"
    running = True

    def __init__(self) -> None:
        self.raised: list[Notification] = []

    async def raise_notification(self, notification: Notification) -> bool:
        self.raised.append(notification)
        return True


def configure(ctx: NovaContext, *, camera_name: str = "bedroom", **security_overrides: Any) -> None:
    ctx.store.patch(
        {"vision": {"named_cameras": [{"name": camera_name, "index": 0}]}}, persist=False
    )
    ctx.store.patch({"security": {"camera_name": camera_name, **security_overrides}}, persist=False)


def make_service(ctx: NovaContext) -> SecurityService:
    service = SecurityService(ctx)
    # Real cv2 is not installed in this environment (and should not need to
    # be, to test this decision logic) — a truthy sentinel is enough to
    # satisfy `.loaded` and skip the real load() path.
    service.engine._detector = object()
    service.engine._recogniser = object()
    return service


@pytest.fixture(autouse=True)
def fake_pool(monkeypatch: pytest.MonkeyPatch):
    """Every test in this file exercises the decision logic around a camera
    read, never a real one — autouse so nothing accidentally falls through
    to actually opening /dev/video0."""
    frames: list[Any] = []

    async def fake_read_bgr(index: int) -> Any:
        return frames.pop(0) if frames else "frame"

    monkeypatch.setattr(local_camera_pool, "read_bgr", fake_read_bgr)
    return frames


@pytest.fixture(autouse=True)
def fake_ntfy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_send_ntfy(server: str, topic: str, **kwargs: Any) -> bool:
        calls.append({"server": server, "topic": topic, **kwargs})
        return True

    monkeypatch.setattr(security_service_module, "send_ntfy", fake_send_ntfy)
    return calls


def known_face() -> FaceObservation:
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=[1.0, 0.0, 0.0, 0.0])


def unknown_face() -> FaceObservation:
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=[0.0, 1.0, 0.0, 0.0])


# -------------------------------------------------------------------- arm


async def test_arm_refuses_without_a_configured_camera(ctx: NovaContext) -> None:
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])

    with pytest.raises(SkillError, match="configured camera"):
        await service.arm()


async def test_arm_refuses_without_an_enrolled_face(ctx: NovaContext) -> None:
    configure(ctx)
    service = make_service(ctx)

    with pytest.raises(SkillError, match="no face is enrolled"):
        await service.arm()


async def test_arm_succeeds_and_starts_watching(ctx: NovaContext, fake_pool: list[Any]) -> None:
    configure(ctx)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])

    result = await service.arm()

    assert result == "Watching now."
    assert service.armed is True
    await service.disarm()


async def test_arming_generates_the_ntfy_topic_immediately(
    ctx: NovaContext, fake_pool: list[Any]
) -> None:
    """The topic must exist as soon as room-watch is armed for the first
    time, not only after the first alert — otherwise there is no way to
    know what to subscribe to on ntfy before something actually happens."""
    configure(ctx)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    assert ctx.settings.security.ntfy_topic == ""

    await service.arm()

    assert ctx.settings.security.ntfy_topic != ""
    await service.disarm()


async def test_arming_twice_is_a_no_op_message(ctx: NovaContext, fake_pool: list[Any]) -> None:
    configure(ctx)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    await service.arm()

    result = await service.arm()

    assert result == "Already watching."
    await service.disarm()


async def test_disarm_stops_the_watch_task(ctx: NovaContext, fake_pool: list[Any]) -> None:
    configure(ctx)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    await service.arm()

    result = await service.disarm()

    assert result == "Stood down."
    assert service.armed is False


async def test_disarming_when_not_armed_is_a_no_op_message(ctx: NovaContext) -> None:
    service = make_service(ctx)
    assert await service.disarm() == "Already stood down."


# --------------------------------------------------------- one check cycle


async def test_a_recognised_face_raises_no_alert(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    configure(ctx)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    voice = FakeVoice()
    ctx.services.register(voice)  # type: ignore[arg-type]
    service.engine.observe = lambda frame: [known_face()]  # type: ignore[method-assign]

    await service._check_once(0)

    assert voice.spoken == []
    assert fake_ntfy == []


async def test_an_empty_frame_raises_no_alert(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    configure(ctx)
    service = make_service(ctx)
    service.engine.observe = lambda frame: []  # type: ignore[method-assign]

    await service._check_once(0)

    assert fake_ntfy == []


async def test_a_single_unknown_frame_does_not_yet_alert(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    """confirm_frames defaults to 2 — one odd frame must not be enough."""
    configure(ctx)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    service.engine.observe = lambda frame: [unknown_face()]  # type: ignore[method-assign]

    await service._check_once(0)

    assert fake_ntfy == []
    assert service._consecutive_unknown == 1


async def test_the_first_ever_alert_fires_on_a_freshly_booted_clock(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: time.monotonic() counts from an arbitrary reference point
    (often boot time), not the epoch. A machine that has been up for less
    than alert_cooldown_seconds must still raise its first-ever alert — this
    pins that down with a clock that is explicitly still near zero, rather
    than relying on this environment's own uptime to happen to exceed the
    cooldown, which is exactly what let this slip through twice before."""
    monkeypatch.setattr(security_service_module.time, "monotonic", lambda: 5.0)
    configure(ctx, confirm_frames=1, alert_cooldown_seconds=120)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    service.engine.observe = lambda frame: [unknown_face()]  # type: ignore[method-assign]

    await service._check_once(0)

    assert len(fake_ntfy) == 1


async def test_consecutive_unknown_frames_trigger_every_channel(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    configure(ctx, alert_message="You should not be in here. Fin has been alerted.")
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    voice = FakeVoice()
    notifications = FakeNotifications()
    ctx.services.register(voice)  # type: ignore[arg-type]
    ctx.services.register(notifications)  # type: ignore[arg-type]
    service.engine.observe = lambda frame: [unknown_face()]  # type: ignore[method-assign]

    await service._check_once(0)  # 1st: not enough yet
    await service._check_once(0)  # 2nd: confirm_frames reached

    assert voice.spoken == ["You should not be in here. Fin has been alerted."]
    assert len(notifications.raised) == 1
    assert notifications.raised[0].body == "You should not be in here. Fin has been alerted."
    assert len(fake_ntfy) == 1
    assert fake_ntfy[0]["message"] == "You should not be in here. Fin has been alerted."


async def test_recognising_someone_resets_the_confirm_counter(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    """A stranger, then the resident walking into frame, then the stranger
    again must not silently add up to two consecutive unknown sightings."""
    configure(ctx)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    frames = [[unknown_face()], [known_face()], [unknown_face()]]

    def observe(frame: Any) -> list[FaceObservation]:
        return frames.pop(0)

    service.engine.observe = observe  # type: ignore[method-assign]

    await service._check_once(0)
    await service._check_once(0)
    await service._check_once(0)

    assert fake_ntfy == []
    assert service._consecutive_unknown == 1


async def test_a_second_alert_within_the_cooldown_is_suppressed(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    configure(ctx, confirm_frames=1, alert_cooldown_seconds=3600)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    service.engine.observe = lambda frame: [unknown_face()]  # type: ignore[method-assign]

    await service._check_once(0)
    await service._check_once(0)

    assert len(fake_ntfy) == 1  # not 2 — the second sighting is still in cooldown


async def test_a_camera_read_failure_does_not_crash_the_check(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(ctx)
    service = make_service(ctx)

    async def failing_read(index: int) -> Any:
        raise RuntimeError("camera unplugged")

    monkeypatch.setattr(local_camera_pool, "read_bgr", failing_read)

    await service._check_once(0)  # must not raise

    assert fake_ntfy == []


async def test_ntfy_topic_is_generated_once_and_reused(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    configure(ctx, confirm_frames=1)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    service.engine.observe = lambda frame: [unknown_face()]  # type: ignore[method-assign]

    await service._check_once(0)
    generated = ctx.settings.security.ntfy_topic
    assert generated  # a topic now exists, unprompted

    # -inf, not 0.0: time.monotonic() counts from an arbitrary reference
    # point, so 0.0 is not reliably "in the past" — see the same reasoning
    # on SecurityService's own _last_alert_at sentinel.
    service._last_alert_at = float("-inf")  # bypass cooldown to trigger a second alert
    await service._check_once(0)

    assert fake_ntfy[0]["topic"] == generated
    assert fake_ntfy[1]["topic"] == generated  # not regenerated on the second alert


async def test_no_click_url_without_a_configured_public_url(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    configure(ctx, confirm_frames=1)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    service.engine.observe = lambda frame: [unknown_face()]  # type: ignore[method-assign]

    await service._check_once(0)

    assert fake_ntfy[0]["click_url"] == ""


async def test_click_url_points_at_the_app_but_never_carries_the_bridge_token(
    ctx: NovaContext, fake_ntfy: list[dict[str, Any]]
) -> None:
    """The click URL is handed to a third-party push relay (ntfy.sh by default),
    so it must never carry `transport.token` — that's the same full-privilege
    secret that gates the entire bridge, not a camera-scoped credential."""
    configure(ctx, confirm_frames=1)
    ctx.store.patch(
        {"transport": {"public_url": "https://box.tailnet.ts.net", "token": "secret"}},
        persist=False,
    )
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])
    service.engine.observe = lambda frame: [unknown_face()]  # type: ignore[method-assign]

    await service._check_once(0)

    click_url = fake_ntfy[0]["click_url"]
    assert click_url == "https://box.tailnet.ts.net/"
    assert "secret" not in click_url
    assert "token" not in click_url


# ------------------------------------------------------------ failure modes


async def test_a_detection_glitch_does_not_stop_the_watch_loop(ctx: NovaContext) -> None:
    """Regression: only the camera read was guarded against raising, so a
    face-detection error (cv2 choking on a malformed frame, a driver hiccup)
    took the whole watch loop down — silently, since this was reached from
    inside `_check_once`, one level below where anything tells the user."""
    configure(ctx, confirm_frames=1)
    service = make_service(ctx)
    service.faces.add("Fin", [1.0, 0.0, 0.0, 0.0])

    def explode(frame: Any) -> Any:
        raise RuntimeError("boom")

    service.engine.observe = explode  # type: ignore[method-assign]

    await service._check_once(0)  # must not raise


async def test_a_watch_loop_crash_tells_the_user_it_stopped(ctx: NovaContext) -> None:
    """Regression: `_watch_loop` caught an unexpected crash and quietly set
    `armed = False` — the person who armed it had no way to know room-watch
    was no longer protecting anything."""
    voice = FakeVoice()
    notifications = FakeNotifications()
    ctx.services.register(voice)  # type: ignore[arg-type]
    ctx.services.register(notifications)  # type: ignore[arg-type]
    service = make_service(ctx)
    service.armed = True

    async def explode(index: int) -> None:
        raise RuntimeError("boom")

    service._check_once = explode  # type: ignore[method-assign]

    await service._watch_loop(0)

    assert service.armed is False
    assert voice.spoken and "stopped" in voice.spoken[0].lower()
    assert notifications.raised and notifications.raised[0].title == "Room-watch stopped"
