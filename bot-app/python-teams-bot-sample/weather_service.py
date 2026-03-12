from __future__ import annotations

import re
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

US_STATE_ABBREVIATIONS = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
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
            place = await self._resolve_place(city_name)
        except Exception as exc:
            return f"Weather lookup failed while geocoding `{city_name}`: {exc}"

        if not place:
            return f"No weather location match found for `{city_name}`."

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

    async def _resolve_place(self, city_name: str) -> dict[str, Any] | None:
        results = await self._geocode(city_name, count=1)
        if results:
            return results[0]

        city_part, region_part = self._split_city_region(city_name)
        if not city_part:
            return None

        fallback_results = await self._geocode(city_part, count=10)
        if not fallback_results:
            return None

        if not region_part:
            return fallback_results[0]

        normalized_region = self._normalize_region(region_part)
        for result in fallback_results:
            admin1 = self._normalize_region(str(result.get("admin1") or ""))
            country_code = str(result.get("country_code") or "").upper()
            if admin1 == normalized_region:
                return result
            if country_code == normalized_region:
                return result

        return fallback_results[0]

    async def _geocode(self, query: str, *, count: int) -> list[dict[str, Any]]:
        geocoding_payload = await self._fetch_json(
            self.GEOCODING_URL,
            {
                "name": query,
                "count": count,
                "language": "en",
                "format": "json",
            },
        )
        return [
            result for result in (geocoding_payload.get("results") or [])
            if isinstance(result, dict)
        ]

    @staticmethod
    def _split_city_region(city_name: str) -> tuple[str, str | None]:
        if "," not in city_name:
            return city_name.strip(), None

        city_part, region_part = city_name.split(",", 1)
        return city_part.strip(), region_part.strip() or None

    @staticmethod
    def _normalize_region(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z]", "", value).upper()
        if not cleaned:
            return ""
        if cleaned in US_STATE_ABBREVIATIONS:
            return re.sub(r"[^A-Za-z]", "", US_STATE_ABBREVIATIONS[cleaned]).upper()
        return cleaned

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