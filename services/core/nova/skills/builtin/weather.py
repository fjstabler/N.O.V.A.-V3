"""Weather: a spoken forecast and a live on-screen surface for the same request.

Uses Open-Meteo (free, no API key, no account) and its own free geocoding, so
this needs nothing configured beyond the assistant's `location` — and even that
is optional, since the tool takes an override.

Same pattern as display: the spoken answer is a short line, and the surface
next to it is the picture — current conditions, the next few hours, and the
week — so "what's the weather" both speaks it and puts it on screen.
"""

from __future__ import annotations

from typing import Annotated, Any

from ...runtime import Topics
from ...runtime.errors import SkillError
from ..base import Param, Skill, tool

#: WMO weather codes → a short English phrase, enough for a spoken reply.
_WEATHER_PHRASES: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def describe_code(code: int) -> str:
    return _WEATHER_PHRASES.get(int(code), "unsettled")


class WeatherSkill(Skill):
    name = "weather"
    description = "Current conditions and forecast — spoken and shown on screen."
    category = "Weather"
    prompt_hint = (
        "For any weather question — 'what's the weather', 'is it going to rain today', 'what's "
        "the forecast for the week' — call get_weather. It both speaks the answer and puts the "
        "forecast on screen; keep the spoken reply short and let the surface show the detail."
    )

    def _unit(self) -> str:
        return "F" if self.ctx.settings.assistant.units == "imperial" else "C"

    @tool(
        "Get the weather for a place — current conditions plus today, the next few hours and the "
        "week ahead. Both speaks a short answer and shows a live forecast on screen. Leave `place` "
        "empty to use the configured location (Assistant → Location)."
    )
    async def get_weather(
        self,
        place: Annotated[
            str, Param("City or address; empty for the configured location", examples=("London",))
        ] = "",
    ) -> str:
        location = (place or self.ctx.settings.assistant.location).strip()
        if not location:
            raise SkillError(
                "I don't have a location — set one under Assistant → Location, or tell me one."
            )
        lat, lon, label = await self._geocode(location)
        forecast = await self._forecast(lat, lon)
        return self._publish_and_speak(label, forecast)

    # -------------------------------------------------------------- publish

    def _publish_and_speak(self, label: str, forecast: dict[str, Any]) -> str:
        unit = self._unit()
        current = forecast["current"]
        hours = forecast["hours"]
        days = forecast["days"]

        self.ctx.bus.publish(
            Topics.UI_SURFACE_SHOW,
            {
                "kind": "weather",
                "title": f"Weather — {label}",
                "place": label,
                "unit": unit,
                "current": current,
                "hours": hours,
                "days": days,
            },
            source=self.name,
        )

        today = days[0] if days else None
        phrase = describe_code(current["code"])
        line = f"{label}: {phrase}, {round(current['temperature'])}°{unit}"
        if today is not None:
            line += f", high {round(today['high'])}°, low {round(today['low'])}°"
        return line + "."

    # -------------------------------------------------------------- fetching

    async def _geocode(self, place: str) -> tuple[float, float, str]:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 1, "format": "json"},
            )
        response.raise_for_status()
        results = (response.json() or {}).get("results") or []
        if not results:
            raise SkillError(f"I couldn't find '{place}' on the map.")
        top = results[0]
        label = ", ".join(
            part for part in (top.get("name"), top.get("admin1"), top.get("country")) if part
        )
        return float(top["latitude"]), float(top["longitude"]), label or place

    async def _forecast(self, lat: float, lon: float) -> dict[str, Any]:
        import httpx

        unit = "fahrenheit" if self._unit() == "F" else "celsius"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,precipitation,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum",
            "forecast_days": 7,
            "temperature_unit": unit,
            "timezone": "auto",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
        response.raise_for_status()
        return _shape_forecast(response.json() or {})


def _shape_forecast(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn Open-Meteo's parallel-array response into the shape the surface expects."""
    from datetime import datetime, time

    current_raw = raw.get("current") or {}
    current = {
        "temperature": float(current_raw.get("temperature_2m", 0.0)),
        "feelsLike": float(
            current_raw.get("apparent_temperature", current_raw.get("temperature_2m", 0.0))
        ),
        "code": int(current_raw.get("weather_code", 0)),
        "wind": float(current_raw.get("wind_speed_10m", 0.0)),
    }

    hourly = raw.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    precs = hourly.get("precipitation") or []
    codes = hourly.get("weather_code") or []

    # Only future hours, next 12.
    now = datetime.now()
    hours = []
    # strict=False on purpose: Open-Meteo occasionally returns arrays whose
    # tail is shorter for the newest hour — better to truncate than to crash.
    for iso, temp, prec, code in zip(times, temps, precs, codes, strict=False):
        try:
            when = datetime.fromisoformat(iso)
        except ValueError:
            continue
        if when < now.replace(minute=0, second=0, microsecond=0):
            continue
        hours.append(
            {
                "time": iso,
                "temperature": float(temp),
                "precipitation": float(prec or 0.0),
                "code": int(code),
            }
        )
        if len(hours) >= 12:
            break

    daily = raw.get("daily") or {}
    d_times = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    d_codes = daily.get("weather_code") or []
    d_precs = daily.get("precipitation_sum") or []
    days = []
    for iso, high, low, code, prec in zip(d_times, highs, lows, d_codes, d_precs, strict=False):
        try:
            day = datetime.combine(datetime.fromisoformat(iso).date(), time.min)
        except ValueError:
            continue
        days.append(
            {
                "date": iso,
                "label": day.strftime("%a %d %b"),
                "high": float(high),
                "low": float(low),
                "code": int(code),
                "precipitation": float(prec or 0.0),
            }
        )
    return {"current": current, "hours": hours, "days": days}
