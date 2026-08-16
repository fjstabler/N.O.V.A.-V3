"""PresenceService: deciding where the user is, and reaching them there.

Presence is layered so it works on any hardware: a recent interaction is enough
on its own, a camera glance is only consulted when that is cold, and when neither
can answer it says so rather than guessing "present". Routing then follows the
reading — spoken in the room, pushed to the phone when away — and the camera path
is faked throughout, so none of this needs OpenCV, a webcam or the network.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from nova.context import NovaContext
from nova.notifications import Notification
from nova.presence import Presence, PresenceService
from nova.runtime import Topics
from nova.runtime.service import Service, ServiceState

# ----------------------------------------------------------- fake services


class FakeVoice(Service):
    name = "voice"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.spoken: list[str] = []

    async def speak(self, text: str) -> None:
        self.spoken.append(text)


class FakeNotifications(Service):
    name = "notifications"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.raised: list[Notification] = []

    async def raise_notification(self, note: Notification) -> bool:
        self.raised.append(note)
        return True


def register_running(ctx: NovaContext, service: Service) -> Service:
    ctx.services.register(service)
    service._set_state(ServiceState.RUNNING)
    return service


def make_presence(ctx: NovaContext) -> PresenceService:
    service = PresenceService(ctx)
    ctx.services.register(service)
    service._set_state(ServiceState.RUNNING)
    return service


# --------------------------------------------------------------- detection


async def test_a_recent_interaction_means_present_without_a_camera(ctx: NovaContext) -> None:
    presence = make_presence(ctx)
    presence._note_interaction(None)  # "you just spoke to me"

    result = await presence.is_present()

    assert result.present is True
    assert result.method == "interaction"


async def test_an_old_interaction_no_longer_counts(ctx: NovaContext) -> None:
    ctx.store.patch({"presence": {"use_camera": False}}, persist=False)
    presence = make_presence(ctx)
    # Push the last interaction outside the window.
    window = ctx.settings.presence.interaction_window_seconds
    presence._last_interaction = time.monotonic() - window - 1

    result = await presence.is_present()

    assert result.present is False
    assert result.method == "unknown"


async def test_the_camera_confirms_presence_when_interaction_is_cold(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    presence = make_presence(ctx)

    async def seen(self: PresenceService) -> bool | None:
        return True

    monkeypatch.setattr(PresenceService, "_camera_shows_known_face", seen)

    result = await presence.is_present()

    assert result.present is True
    assert result.method == "camera"


async def test_the_camera_reports_absence_when_no_known_face(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    presence = make_presence(ctx)

    async def not_seen(self: PresenceService) -> bool | None:
        return False

    monkeypatch.setattr(PresenceService, "_camera_shows_known_face", not_seen)

    result = await presence.is_present()

    assert result.present is False
    assert result.method == "camera"


async def test_use_camera_off_skips_the_camera_entirely(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"presence": {"use_camera": False}}, persist=False)
    presence = make_presence(ctx)

    async def boom(self: PresenceService) -> bool | None:
        raise AssertionError("camera must not be consulted when use_camera is off")

    monkeypatch.setattr(PresenceService, "_camera_shows_known_face", boom)

    result = await presence.is_present()

    assert result.method == "unknown"


async def test_an_armed_room_watch_provides_presence(ctx: NovaContext) -> None:
    """A watch that just saw an enrolled face is a free presence signal."""
    ctx.store.patch({"presence": {"use_camera": False}}, persist=False)

    class FakeSecurity(Service):
        name = "security"

    security = register_running(ctx, FakeSecurity(ctx))
    security.last_known_face_at = time.monotonic()  # type: ignore[attr-defined]
    presence = make_presence(ctx)

    result = await presence.is_present()

    assert result.present is True
    assert result.method == "watch"


async def test_the_reading_is_cached_briefly(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    presence = make_presence(ctx)
    calls = {"n": 0}

    async def counting(self: PresenceService) -> Presence:
        calls["n"] += 1
        return Presence(False, "unknown")

    monkeypatch.setattr(PresenceService, "_determine", counting)

    await presence.is_present()
    await presence.is_present()

    assert calls["n"] == 1  # second call served from cache


async def test_a_fresh_interaction_invalidates_the_cache(ctx: NovaContext) -> None:
    presence = make_presence(ctx)
    # Seed a stale "absent" cache, then interact — the next read must not be stale.
    presence._cache = Presence(False, "unknown")
    presence._cache_at = time.monotonic()

    presence._note_interaction(None)
    result = await presence.is_present()

    assert result.present is True


async def test_interaction_events_update_presence_through_the_bus(ctx: NovaContext) -> None:
    presence = make_presence(ctx)
    await presence.on_start()  # subscribe to the interaction topics

    ctx.bus.publish(Topics.TURN_STARTED, {}, source="test")
    result = await presence.is_present()

    assert result.present is True
    assert result.method == "interaction"


# ----------------------------------------------------------------- routing


async def test_reach_user_speaks_when_present(ctx: NovaContext) -> None:
    voice = register_running(ctx, FakeVoice(ctx))
    notifications = register_running(ctx, FakeNotifications(ctx))
    presence = make_presence(ctx)
    presence._note_interaction(None)  # present

    result = await presence.reach_user("Reminder", "call mum")

    assert voice.spoken == ["Reminder. call mum"]
    assert len(notifications.raised) == 1
    assert notifications.raised[0].speak is False  # already spoken, don't double up
    assert "out loud" in result


async def test_reach_user_pushes_to_the_phone_when_away(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"presence": {"use_camera": False}}, persist=False)
    voice = register_running(ctx, FakeVoice(ctx))
    notifications = register_running(ctx, FakeNotifications(ctx))
    presence = make_presence(ctx)  # no interaction → away

    sent: list[dict[str, Any]] = []

    async def fake_send(server: str, topic: str, **kwargs: Any) -> bool:
        sent.append({"server": server, "topic": topic, **kwargs})
        return True

    monkeypatch.setattr("nova.presence.send_ntfy", fake_send)

    result = await presence.reach_user("Laundry", "the wash is done")

    assert voice.spoken == []  # not spoken to an empty room
    assert len(sent) == 1
    assert sent[0]["title"] == "Laundry"
    assert len(notifications.raised) == 1  # still left on screen
    assert "phone" in result


async def test_push_is_skipped_when_disabled(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch(
        {"presence": {"use_camera": False}, "notifications": {"push_enabled": False}},
        persist=False,
    )
    register_running(ctx, FakeNotifications(ctx))
    presence = make_presence(ctx)

    async def fake_send(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("push must not be attempted when disabled")

    monkeypatch.setattr("nova.presence.send_ntfy", fake_send)

    result = await presence.reach_user("Heads up", "something happened")

    assert "off" in result.lower()


def test_ensure_push_topic_generates_and_persists_a_private_topic(ctx: NovaContext) -> None:
    presence = make_presence(ctx)
    assert ctx.settings.notifications.push_topic == ""

    topic = presence._ensure_push_topic()

    assert topic.startswith("nova-")
    assert ctx.settings.notifications.push_topic == topic
    # Stable across calls.
    assert presence._ensure_push_topic() == topic
