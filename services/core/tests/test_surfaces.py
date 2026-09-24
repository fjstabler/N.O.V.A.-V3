"""In-app surfaces: the tools that both speak an answer and show it on screen.

Every "put it on screen" tool publishes to Topics.UI_SURFACE_SHOW alongside its
spoken reply. Those events are captured here to check that (a) the surface fires,
(b) it carries the shape the frontend expects, and (c) speaking still works.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from nova.context import NovaContext
from nova.integrations.calendar import Event
from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.integrations.services import CalendarService, HomeService
from nova.runtime import Topics
from nova.runtime.service import ServiceState
from nova.skills.builtin.home import HomeSkill
from nova.skills.builtin.schedule import CalendarSkill, _start_of_week
from nova.skills.builtin.weather import WeatherSkill


def captured(ctx: NovaContext) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ctx.bus.subscribe(Topics.UI_SURFACE_SHOW, lambda e: events.append(e.payload))
    return events


# ------------------------------------------------------------------ agenda


async def make_calendar(ctx: NovaContext) -> CalendarService:
    ctx.store.patch({"calendar": {"enabled": True}}, persist=False)
    service = CalendarService(ctx)
    ctx.services.register(service)
    await service.start()
    return service


async def test_week_ahead_publishes_an_agenda_surface(ctx: NovaContext) -> None:
    service = await make_calendar(ctx)
    monday = _start_of_week(datetime.now())
    tuesday = (monday + timedelta(days=1)).replace(hour=10)
    await service.create(
        Event(summary="Standup", starts_at=tuesday.timestamp(), ends_at=tuesday.timestamp() + 1800)
    )
    events = captured(ctx)

    reply = await CalendarSkill(ctx).week_ahead(start="this week")

    assert reply  # still spoken
    assert len(events) == 1
    payload = events[0]
    assert payload["kind"] == "agenda"
    assert payload["title"].lower().startswith("this week")
    # Seven days, every one listed even if empty.
    assert len(payload["days"]) == 7
    tuesday_bucket = next(
        d for d in payload["days"] if d["date"] == (monday + timedelta(days=1)).date().isoformat()
    )
    assert tuesday_bucket["events"][0]["summary"] == "Standup"
    assert tuesday_bucket["events"][0]["allDay"] is False


async def test_agenda_publishes_the_single_day(ctx: NovaContext) -> None:
    await make_calendar(ctx)
    events = captured(ctx)
    await CalendarSkill(ctx).agenda(when="today")
    assert events and events[0]["kind"] == "agenda"
    assert len(events[0]["days"]) == 1


# --------------------------------------------------------------- overview


def home_with(ctx: NovaContext, *entities: HAEntity) -> HomeService:
    service = HomeService(ctx)
    ctx.services.register(service)
    client = HomeAssistantClient("http://ha.local:8123", "token")
    for entity in entities:
        client._entities[entity.entity_id] = entity
    service.ha = client
    service._set_state(ServiceState.RUNNING)
    return service


async def test_home_overview_publishes_a_surface(ctx: NovaContext) -> None:
    home_with(
        ctx,
        HAEntity(
            entity_id="light.kitchen",
            state="on",
            attributes={
                "friendly_name": "Kitchen Light",
                "area": "Kitchen",
                "brightness": 128,
            },
        ),
        HAEntity(
            entity_id="climate.hallway",
            state="heat",
            attributes={"friendly_name": "Thermostat", "current_temperature": 19.5},
        ),
        HAEntity(
            entity_id="lock.front",
            state="unlocked",
            attributes={"friendly_name": "Front Door"},
        ),
        HAEntity(
            entity_id="camera.doorbell",
            state="idle",
            attributes={"friendly_name": "Doorbell"},
        ),
        # Two intentionally-not-on entities: a motion sensor reading "on" and a
        # media player in standby — both would have been wrongly counted before.
        HAEntity(
            entity_id="binary_sensor.hall_motion",
            state="on",
            attributes={"friendly_name": "Hall motion", "device_class": "motion"},
        ),
        HAEntity(
            entity_id="media_player.tv",
            state="standby",
            attributes={"friendly_name": "TV"},
        ),
    )
    events = captured(ctx)

    reply = await HomeSkill(ctx).home_overview()

    assert "1 on" in reply  # only the light — not the motion sensor, not the standby TV
    assert len(events) == 1
    payload = events[0]
    assert payload["kind"] == "home-overview"
    on_names = [d["name"] for d in payload["on"]]
    assert on_names == ["Kitchen Light"]
    assert payload["on"][0]["domain"] == "light"
    assert payload["on"][0]["area"] == "Kitchen"
    assert payload["on"][0]["detail"] == "50%"  # brightness rendered as percent
    assert payload["climate"][0]["detail"] == "19.5°"
    assert payload["unlocked"][0]["name"] == "Front Door"
    assert payload["cameras"][0]["streamPath"] == "/camera/ha:camera.doorbell"


async def test_media_player_playing_counts_as_on(ctx: NovaContext) -> None:
    home_with(
        ctx,
        HAEntity(
            entity_id="media_player.tv",
            state="playing",
            attributes={"friendly_name": "TV", "media_title": "The News"},
        ),
    )
    events = captured(ctx)
    await HomeSkill(ctx).home_overview()
    on = events[0]["on"]
    assert [d["name"] for d in on] == ["TV"]
    assert on[0]["detail"] == "The News"


# --------------------------------------------------------------- weather


def _fake_weather_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "London",
                            "admin1": "England",
                            "country": "UK",
                            "latitude": 51.5,
                            "longitude": -0.13,
                        }
                    ]
                },
            )
        # Forecast: current + 3 future hours + 3 days.
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        hourly_times = [(now + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(3)]
        daily_dates = [(now + timedelta(days=i)).date().isoformat() for i in range(3)]
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 12.0,
                    "apparent_temperature": 10.5,
                    "weather_code": 3,
                    "wind_speed_10m": 8.0,
                },
                "hourly": {
                    "time": hourly_times,
                    "temperature_2m": [12.0, 11.5, 11.0],
                    "precipitation": [0.0, 0.2, 0.5],
                    "weather_code": [3, 61, 61],
                },
                "daily": {
                    "time": daily_dates,
                    "temperature_2m_max": [14.0, 15.0, 16.0],
                    "temperature_2m_min": [8.0, 9.0, 10.0],
                    "weather_code": [3, 61, 1],
                    "precipitation_sum": [0.0, 3.4, 0.0],
                },
            },
        )

    return httpx.MockTransport(handler)


async def test_get_weather_speaks_and_publishes(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"assistant": {"location": "London"}}, persist=False)
    events = captured(ctx)

    transport = _fake_weather_transport()
    original_ctor = httpx.AsyncClient.__init__

    def with_transport(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_ctor(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", with_transport)

    reply = await WeatherSkill(ctx).get_weather()

    assert "London" in reply
    assert "°" in reply
    assert len(events) == 1
    payload = events[0]
    assert payload["kind"] == "weather"
    assert payload["place"].startswith("London")
    assert payload["unit"] == "C"
    assert payload["current"]["temperature"] == 12.0
    assert len(payload["days"]) == 3
    assert 1 <= len(payload["hours"]) <= 12


# ------------------------------------------------------------- system rings

from types import SimpleNamespace  # noqa: E402 — grouped with the block that uses it

from nova.skills.builtin.server import _build_system_rings  # noqa: E402


def _fake_metrics(**over: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "cpu_percent": 12.0,
        "memory_percent": 47.0,
        "memory_used_gb": 7.5,
        "memory_total_gb": 16.0,
        "temperatures": {"Package id 0": 58.0, "Core 0": 55.0},
        "disks": [SimpleNamespace(mountpoint="/", total_gb=500.0, used_gb=200.0, percent=40.0)],
    }
    defaults.update(over)
    return SimpleNamespace(**defaults)


def test_system_rings_shape() -> None:
    rings = _build_system_rings(_fake_metrics())
    labels = [r["label"] for r in rings]
    assert labels == ["CPU temp", "RAM", "Disk free"]
    # Each ring carries a formatted value and a 0..1 fraction.
    for ring in rings:
        assert isinstance(ring["value"], str) and ring["value"]
        assert 0.0 <= float(ring["fraction"]) <= 1.0
        assert ring["tone"] in {"ok", "warn", "critical"}


def test_system_rings_cpu_temp_scales_and_tones() -> None:
    ok = _build_system_rings(_fake_metrics(temperatures={"cpu": 50.0}))[0]
    warn = _build_system_rings(_fake_metrics(temperatures={"cpu": 78.0}))[0]
    hot = _build_system_rings(_fake_metrics(temperatures={"cpu": 92.0}))[0]
    assert ok["value"] == "50°C" and ok["tone"] == "ok"
    assert warn["tone"] == "warn"
    assert hot["tone"] == "critical"
    # 30° maps to 0.0, 90° to 1.0
    assert 0.0 < ok["fraction"] < 1.0
    assert hot["fraction"] == 1.0


def test_system_rings_fall_back_to_cpu_load_without_temps() -> None:
    ring = _build_system_rings(_fake_metrics(temperatures={}, cpu_percent=42.0))[0]
    assert ring["label"] == "CPU load"
    assert ring["value"] == "42%"


def test_system_rings_disk_shows_free_pct_and_gb() -> None:
    disk = SimpleNamespace(mountpoint="/", total_gb=1000.0, used_gb=900.0, percent=90.0)
    ring = _build_system_rings(_fake_metrics(disks=[disk]))[2]
    assert ring["value"] == "10%"  # free
    assert ring["fraction"] == 0.9  # arc shows what's used
    assert "100 GB free" in ring["caption"]
    assert ring["tone"] == "warn"  # 90% used is worrying; 92%+ escalates to critical

    critical = SimpleNamespace(mountpoint="/", total_gb=1000.0, used_gb=950.0, percent=95.0)
    ring = _build_system_rings(_fake_metrics(disks=[critical]))[2]
    assert ring["tone"] == "critical"


def test_system_rings_disk_picks_the_largest_mount() -> None:
    small = SimpleNamespace(mountpoint="/boot", total_gb=1.0, used_gb=0.5, percent=50.0)
    big = SimpleNamespace(mountpoint="/", total_gb=500.0, used_gb=100.0, percent=20.0)
    ring = _build_system_rings(_fake_metrics(disks=[small, big]))[2]
    assert "500" in ring["caption"]  # picked / not /boot


async def test_system_rings_tool_publishes_a_surface(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool speaks a short line AND publishes a system-stats surface."""
    from nova.runtime.service import ServiceState
    from nova.skills.builtin.server import ServerSkill
    from nova.system.service import SystemService

    ctx.store.patch({"server": {"enabled": True}}, persist=False)
    system = SystemService(ctx)
    ctx.services.register(system)
    system._set_state(ServiceState.RUNNING)

    fake = _fake_metrics()

    async def fake_current_metrics() -> Any:
        return fake

    monkeypatch.setattr(system, "current_metrics", fake_current_metrics)
    events = captured(ctx)

    reply = await ServerSkill(ctx).system_rings()

    assert "CPU" in reply and "RAM" in reply and "Disk" in reply
    assert len(events) == 1
    payload = events[0]
    assert payload["kind"] == "system-stats"
    assert len(payload["rings"]) == 3
