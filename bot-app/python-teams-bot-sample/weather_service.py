from __future__ import annotations

from typing import Any, Awaitable, Callable

JsonFetcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


WEATHER_CODE_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherService:
    """Retrieve current weather for a city using the Open-Meteo APIs."""

    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, fetch_json: JsonFetcher | None = None):
        self._fetch_json = fetch_json or self._default_fetch_json

    async def get_weather_text(self, city: str | None) -> str:
        city_name = (city or "").strip()
        if not city_name:
            return "Usage: `weather <city>`"

        try:
            geocoding_payload = await self._fetch_json(
                self.GEOCODING_URL,
                {
                    "name": city_name,
                    "count": 1,
                    "language": "en",
                    "format": "json",
                },
            )
        except Exception as exc:
            return f"Weather lookup failed while geocoding `{city_name}`: {exc}"

        results = geocoding_payload.get("results") or []
        if not results:
            return f"No weather location match found for `{city_name}`."

        place = results[0]
        display_name = self._format_place(place)

        try:
            forecast_payload = await self._fetch_json(
                self.FORECAST_URL,
                {
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": ",".join([
                        "temperature_2m",
                        "apparent_temperature",
                        "relative_humidity_2m",
                        "precipitation",
                        "weather_code",
                        "wind_speed_10m",
                        "is_day",
                    ]),
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
        except Exception as exc:
            return f"Weather lookup failed while fetching current conditions for `{display_name}`: {exc}"

        current = forecast_payload.get("current") or {}
        units = forecast_payload.get("current_units") or {}
        weather_code = current.get("weather_code")
        condition = WEATHER_CODE_DESCRIPTIONS.get(weather_code, "Unknown conditions")

        lines = [
            f"🌦️ Weather for `{display_name}`",
            f"Condition: {condition}",
            (
                "Temperature: "
                f"{current.get('temperature_2m', 'n/a')}{units.get('temperature_2m', '')}"
                f" (feels like {current.get('apparent_temperature', 'n/a')}{units.get('apparent_temperature', '')})"
            ),
            (
                "Humidity: "
                f"{current.get('relative_humidity_2m', 'n/a')}{units.get('relative_humidity_2m', '')}"
            ),
            (
                "Wind: "
                f"{current.get('wind_speed_10m', 'n/a')}{units.get('wind_speed_10m', '')}"
            ),
            (
                "Precipitation: "
                f"{current.get('precipitation', 'n/a')}{units.get('precipitation', '')}"
            ),
        ]

        observed_at = current.get("time")
        if observed_at:
            lines.append(f"Observed at: {observed_at}")

        return "<br>".join(lines)

    async def _default_fetch_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                payload = await response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Unexpected weather API response shape")
                return payload

    @staticmethod
    def _format_place(place: dict[str, Any]) -> str:
        parts = [place.get("name")]
        admin = place.get("admin1")
        country = place.get("country")
        if admin and admin != place.get("name"):
            parts.append(admin)
        if country:
            parts.append(country)
        return ", ".join(part for part in parts if part)