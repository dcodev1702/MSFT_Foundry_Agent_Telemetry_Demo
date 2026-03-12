from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

JsonFetcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
WeatherNarrator = Callable[[dict[str, Any]], Awaitable[str]]


logger = logging.getLogger(__name__)


TRANSIENT_WEATHER_STATUS_CODES = {408, 429, 500, 502, 503, 504}
WEATHER_API_RETRY_ATTEMPTS = 3
WEATHER_API_RETRY_BACKOFF_SECONDS = 0.5


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

COUNTRY_ALIASES = {
    "DE": "GERMANY",
    "DEU": "GERMANY",
    "DEUTSCHLAND": "GERMANY",
    "GERMANY": "GERMANY",
    "UK": "UNITEDKINGDOM",
    "GB": "UNITEDKINGDOM",
    "GBR": "UNITEDKINGDOM",
    "UNITEDKINGDOM": "UNITEDKINGDOM",
    "US": "UNITEDSTATES",
    "USA": "UNITEDSTATES",
    "UNITEDSTATES": "UNITEDSTATES",
    "UNITEDSTATESOFAMERICA": "UNITEDSTATES",
}


@dataclass(slots=True)
class WeatherSnapshot:
    query: str
    display_name: str
    place: dict[str, Any]
    current: dict[str, Any]
    units: dict[str, Any]
    condition: str

    def to_llm_payload(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "display_name": self.display_name,
            "source": "Open-Meteo current conditions",
            "place": {
                "name": self.place.get("name"),
                "admin1": self.place.get("admin1"),
                "country": self.place.get("country"),
                "latitude": self.place.get("latitude"),
                "longitude": self.place.get("longitude"),
            },
            "condition": self.condition,
            "observed_at": self.current.get("time"),
            "temperature": {
                "value": self.current.get("temperature_2m"),
                "unit": self.units.get("temperature_2m"),
            },
            "apparent_temperature": {
                "value": self.current.get("apparent_temperature"),
                "unit": self.units.get("apparent_temperature"),
            },
            "humidity": {
                "value": self.current.get("relative_humidity_2m"),
                "unit": self.units.get("relative_humidity_2m"),
            },
            "wind_speed": {
                "value": self.current.get("wind_speed_10m"),
                "unit": self.units.get("wind_speed_10m"),
            },
            "precipitation": {
                "value": self.current.get("precipitation"),
                "unit": self.units.get("precipitation"),
            },
            "is_day": self.current.get("is_day"),
        }


class WeatherService:
    """Retrieve current weather for a city using the Open-Meteo APIs."""

    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    DEFAULT_OPENAI_API_VERSION = "2024-10-21"
    DEFAULT_OPENAI_MODEL = "gpt-5.3-chat"
    OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"

    def __init__(self, fetch_json: JsonFetcher | None = None, narrator: WeatherNarrator | None = None):
        self._fetch_json = fetch_json or self._default_fetch_json
        self._narrator = narrator
        self._openai_client = None

    async def get_weather_text(self, city: str | None) -> str:
        city_name = (city or "").strip()
        if not city_name:
            return "Usage: `weather <city>`"

        try:
            snapshot = await self._get_weather_snapshot(city_name)
        except RuntimeError as exc:
            return str(exc)

        narrator = self._narrator
        if narrator is None and self._llm_is_configured():
            narrator = self._narrate_with_llm

        if narrator is not None:
            try:
                narrated = (await narrator(snapshot.to_llm_payload())).strip()
                if narrated:
                    return narrated.replace("\n", "<br>")
            except Exception as exc:
                logger.warning("Falling back to deterministic weather formatting for %s: %s", city_name, exc)

        return self._format_snapshot(snapshot)

    async def _get_weather_snapshot(self, city_name: str) -> WeatherSnapshot:
        """Resolve a location and fetch current conditions for it."""

        try:
            place = await self._resolve_place(city_name)
        except Exception as exc:
            raise RuntimeError(f"Weather lookup failed while geocoding `{city_name}`: {exc}") from exc

        if not place:
            raise RuntimeError(f"No weather location match found for `{city_name}`.")

        display_name = self._format_place(place)

        try:
            forecast_payload = await self._fetch_json_with_retry(
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
            raise RuntimeError(
                f"Weather lookup failed while fetching current conditions for `{display_name}`: {exc}"
            ) from exc

        current = forecast_payload.get("current") or {}
        units = forecast_payload.get("current_units") or {}
        weather_code = current.get("weather_code")
        condition = WEATHER_CODE_DESCRIPTIONS.get(weather_code, "Unknown conditions")

        return WeatherSnapshot(
            query=city_name,
            display_name=display_name,
            place=place,
            current=current,
            units=units,
            condition=condition,
        )

    def _format_snapshot(self, snapshot: WeatherSnapshot) -> str:
        current = snapshot.current
        units = snapshot.units

        lines = [
            f"🌦️ Weather for `{snapshot.display_name}`",
            f"Condition: {snapshot.condition}",
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

    def _llm_is_configured(self) -> bool:
        enabled = os.getenv("WEATHER_LLM_ENABLED", "true").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return False
        return bool(os.getenv("WEATHER_LLM_AZURE_OPENAI_ENDPOINT", "").strip())

    async def _narrate_with_llm(self, payload: dict[str, Any]) -> str:
        client = self._get_openai_client()
        response = await client.responses.create(
            model=os.getenv("WEATHER_LLM_MODEL", self.DEFAULT_OPENAI_MODEL).strip() or self.DEFAULT_OPENAI_MODEL,
            instructions=(
                "You are a weather assistant for a Microsoft Teams bot. "
                "Use only the supplied weather JSON. Do not infer, estimate, or invent facts, alerts, or forecasts. "
                "Respond in 4 to 6 concise lines separated by newline characters. "
                "Include the location, current conditions, temperature with feels-like, humidity, wind, precipitation, and a short practical note. "
                "Keep a professional tone and do not use bullet points or markdown tables."
            ),
            input=(
                "Summarize this live weather lookup for the user.\n"
                f"Weather JSON:\n{json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)}"
            ),
            max_output_tokens=220,
        )
        output_text = (getattr(response, "output_text", "") or "").strip()
        if not output_text:
            raise RuntimeError("The weather narration model returned no text.")
        return output_text

    def _get_openai_client(self):
        if self._openai_client is not None:
            return self._openai_client

        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import AsyncAzureOpenAI
        except Exception as exc:
            raise RuntimeError(
                "The `openai` Python package is required for grounded weather narration. Install the updated requirements."
            ) from exc

        managed_identity_client_id = os.getenv("AZURE_CLIENT_ID") or None
        credential = DefaultAzureCredential(managed_identity_client_id=managed_identity_client_id)
        token_provider = get_bearer_token_provider(credential, self.OPENAI_SCOPE)
        self._openai_client = AsyncAzureOpenAI(
            api_version=os.getenv("WEATHER_LLM_API_VERSION", self.DEFAULT_OPENAI_API_VERSION).strip(),
            azure_endpoint=os.environ["WEATHER_LLM_AZURE_OPENAI_ENDPOINT"].strip(),
            azure_ad_token_provider=token_provider,
        )
        return self._openai_client

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

        city_part, location_hints = self._split_location_hints(city_name)
        if not city_part:
            return None

        fallback_results = await self._geocode(city_part, count=10)
        if not fallback_results:
            return None

        if not location_hints:
            return fallback_results[0]

        ranked_results = sorted(
            fallback_results,
            key=lambda result: self._score_place_match(result, location_hints),
            reverse=True,
        )
        return ranked_results[0]

    async def _geocode(self, query: str, *, count: int) -> list[dict[str, Any]]:
        geocoding_payload = await self._fetch_json_with_retry(
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

    async def _fetch_json_with_retry(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, WEATHER_API_RETRY_ATTEMPTS + 1):
            try:
                return await self._fetch_json(url, params)
            except Exception as exc:
                last_error = exc
                if attempt >= WEATHER_API_RETRY_ATTEMPTS or not self._is_retryable_weather_error(exc):
                    raise
                logger.warning(
                    "Retrying weather API request to %s after attempt %s/%s failed: %s",
                    url,
                    attempt,
                    WEATHER_API_RETRY_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(WEATHER_API_RETRY_BACKOFF_SECONDS * attempt)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Weather API request failed without an exception.")

    @staticmethod
    def _is_retryable_weather_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, OSError)):
            return True

        status = getattr(exc, "status", None)
        if isinstance(status, int) and status in TRANSIENT_WEATHER_STATUS_CODES:
            return True

        return False

    @staticmethod
    def _split_location_hints(city_name: str) -> tuple[str, list[str]]:
        parts = [part.strip() for part in city_name.split(",") if part.strip()]
        if not parts:
            return "", []
        return parts[0], parts[1:]

    @staticmethod
    def _normalize_location_token(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z]", "", value).upper()
        if not cleaned:
            return ""
        if cleaned in US_STATE_ABBREVIATIONS:
            return re.sub(r"[^A-Za-z]", "", US_STATE_ABBREVIATIONS[cleaned]).upper()
        if cleaned in COUNTRY_ALIASES:
            return COUNTRY_ALIASES[cleaned]
        return cleaned

    @classmethod
    def _score_place_match(cls, place: dict[str, Any], location_hints: list[str]) -> tuple[int, int, int]:
        normalized_hints = [cls._normalize_location_token(hint) for hint in location_hints if hint.strip()]
        normalized_fields = {
            cls._normalize_location_token(str(place.get("name") or "")),
            cls._normalize_location_token(str(place.get("admin1") or "")),
            cls._normalize_location_token(str(place.get("admin2") or "")),
            cls._normalize_location_token(str(place.get("admin3") or "")),
            cls._normalize_location_token(str(place.get("admin4") or "")),
            cls._normalize_location_token(str(place.get("country") or "")),
            cls._normalize_location_token(str(place.get("country_code") or "")),
        }

        matches = sum(1 for hint in normalized_hints if hint and hint in normalized_fields)
        feature_code = str(place.get("feature_code") or "")
        feature_bonus = 1 if feature_code.startswith("PPL") or feature_code == "ADM1" else 0
        population = int(place.get("population") or 0)
        return matches, feature_bonus, population

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