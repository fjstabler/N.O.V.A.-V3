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
            attributes={"friendly_name": "Kitchen Light", "area": "Kitchen"},
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
    )
    events = captured(ctx)

    reply = await HomeSkill(ctx).home_overview()

    assert "on" in reply.lower()  # still spoken
    assert len(events) == 1
    payload = events[0]
    assert payload["kind"] == "home-overview"
    assert [d["name"] for d in payload["on"]] == ["Kitchen Light"]
    assert payload["climate"][0]["detail"] == "19.5°"
    assert payload["unlocked"][0]["name"] == "Front Door"


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
