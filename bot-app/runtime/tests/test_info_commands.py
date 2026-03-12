from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

azure_module = sys.modules.setdefault("azure", types.ModuleType("azure"))
storage_module = sys.modules.setdefault("azure.storage", types.ModuleType("azure.storage"))
queue_module = sys.modules.setdefault("azure.storage.queue", types.ModuleType("azure.storage.queue"))


class _QueueClient:
    pass


queue_module.QueueClient = _QueueClient
storage_module.queue = queue_module
azure_module.storage = storage_module

from command_parser import parse_command
from msft_docs_service import MicrosoftLearnMcpService
from weather_service import WeatherService


class InfoCommandParsingTests(unittest.TestCase):
    def test_parse_weather_command(self) -> None:
        command = parse_command("weather Redmond")

        self.assertEqual(command.kind, "weather")
        self.assertEqual(command.location, "Redmond")
        self.assertIsNone(command.query)

    def test_parse_msft_docs_command(self) -> None:
        command = parse_command("msft_docs how do I authenticate with managed identity")

        self.assertEqual(command.kind, "msft-docs")
        self.assertEqual(command.query, "how do I authenticate with managed identity")
        self.assertIsNone(command.location)

    def test_parse_bare_weather_command(self) -> None:
        command = parse_command("weather")

        self.assertEqual(command.kind, "weather")
        self.assertIsNone(command.location)

    def test_parse_bare_msft_docs_command(self) -> None:
        command = parse_command("msft_docs")

        self.assertEqual(command.kind, "msft-docs")
        self.assertIsNone(command.query)


class WeatherServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_weather_service_formats_current_conditions(self) -> None:
        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            if "geocoding-api" in url:
                return {
                    "results": [
                        {
                            "name": "Redmond",
                            "admin1": "Washington",
                            "country": "United States",
                            "latitude": 47.673988,
                            "longitude": -122.121513,
                        }
                    ]
                }

            return {
                "current": {
                    "temperature_2m": 11.2,
                    "apparent_temperature": 10.5,
                    "relative_humidity_2m": 63,
                    "precipitation": 0.0,
                    "weather_code": 2,
                    "wind_speed_10m": 7.4,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        service = WeatherService(fetch_json=fake_fetch)

        result = await service.get_weather_text("Redmond")

        self.assertIn("Weather for `Redmond, Washington, United States`", result)
        self.assertIn("Condition: Partly cloudy", result)
        self.assertIn("Temperature: 11.2C", result)

    async def test_weather_service_uses_grounded_narrator_when_available(self) -> None:
        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            if "geocoding-api" in url:
                return {
                    "results": [
                        {
                            "name": "Denver",
                            "admin1": "Colorado",
                            "country": "United States",
                            "latitude": 39.7392,
                            "longitude": -104.9903,
                        }
                    ]
                }

            return {
                "current": {
                    "temperature_2m": 18.0,
                    "apparent_temperature": 17.0,
                    "relative_humidity_2m": 40,
                    "precipitation": 0.0,
                    "weather_code": 0,
                    "wind_speed_10m": 14.0,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        captured_payload: dict[str, object] = {}

        async def fake_narrator(payload: dict[str, object]) -> str:
            captured_payload.update(payload)
            return "Weather summary for Denver\nClear and dry right now"

        service = WeatherService(fetch_json=fake_fetch, narrator=fake_narrator)

        result = await service.get_weather_text("Denver")

        self.assertEqual(result, "Weather summary for Denver<br>Clear and dry right now")
        self.assertEqual(captured_payload["display_name"], "Denver, Colorado, United States")
        self.assertEqual(captured_payload["condition"], "Clear sky")

    async def test_weather_service_falls_back_when_narrator_fails(self) -> None:
        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            if "geocoding-api" in url:
                return {
                    "results": [
                        {
                            "name": "Detroit",
                            "admin1": "Michigan",
                            "country": "United States",
                            "latitude": 42.3314,
                            "longitude": -83.0458,
                        }
                    ]
                }

            return {
                "current": {
                    "temperature_2m": 9.0,
                    "apparent_temperature": 7.0,
                    "relative_humidity_2m": 72,
                    "precipitation": 0.2,
                    "weather_code": 61,
                    "wind_speed_10m": 10.0,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        async def failing_narrator(_payload: dict[str, object]) -> str:
            raise RuntimeError("model unavailable")

        service = WeatherService(fetch_json=fake_fetch, narrator=failing_narrator)

        result = await service.get_weather_text("Detroit")

        self.assertIn("Weather for `Detroit, Michigan, United States`", result)
        self.assertIn("Condition: Slight rain", result)

    async def test_weather_service_falls_back_for_city_and_state_abbreviation(self) -> None:
        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            if "geocoding-api" in url:
                name = params["name"]
                if name == "San Diego, CA":
                    return {"generationtime_ms": 1.0}
                if name == "San Diego":
                    return {
                        "results": [
                            {
                                "name": "San Diego",
                                "admin1": "California",
                                "country": "United States",
                                "country_code": "US",
                                "latitude": 32.7157,
                                "longitude": -117.1611,
                            },
                            {
                                "name": "San Diego",
                                "admin1": "Texas",
                                "country": "United States",
                                "country_code": "US",
                                "latitude": 31.0,
                                "longitude": -100.0,
                            },
                        ]
                    }

            return {
                "current": {
                    "temperature_2m": 20.0,
                    "apparent_temperature": 20.0,
                    "relative_humidity_2m": 60,
                    "precipitation": 0.0,
                    "weather_code": 1,
                    "wind_speed_10m": 8.0,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        service = WeatherService(fetch_json=fake_fetch)

        result = await service.get_weather_text("San Diego, CA")

        self.assertIn("Weather for `San Diego, California, United States`", result)

    async def test_weather_service_retries_transient_forecast_failure(self) -> None:
        forecast_attempts = 0

        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            nonlocal forecast_attempts
            if "geocoding-api" in url:
                return {
                    "results": [
                        {
                            "name": "San Diego",
                            "admin1": "California",
                            "country": "United States",
                            "country_code": "US",
                            "latitude": 32.7157,
                            "longitude": -117.1611,
                        }
                    ]
                }

            forecast_attempts += 1
            if forecast_attempts == 1:
                raise TimeoutError("upstream timeout")

            return {
                "current": {
                    "temperature_2m": 20.0,
                    "apparent_temperature": 20.0,
                    "relative_humidity_2m": 60,
                    "precipitation": 0.0,
                    "weather_code": 1,
                    "wind_speed_10m": 8.0,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        service = WeatherService(fetch_json=fake_fetch)

        result = await service.get_weather_text("San Diego, California, United States")

        self.assertEqual(forecast_attempts, 2)
        self.assertIn("Weather for `San Diego, California, United States`", result)

    async def test_weather_service_matches_full_state_name_after_fallback(self) -> None:
        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            if "geocoding-api" in url:
                name = params["name"]
                if name == "New York, New York":
                    return {"generationtime_ms": 1.0}
                if name == "New York":
                    return {
                        "results": [
                            {
                                "name": "New York",
                                "admin1": "New York",
                                "country": "United States",
                                "country_code": "US",
                                "latitude": 40.71427,
                                "longitude": -74.00597,
                            },
                            {
                                "name": "New York",
                                "admin1": "Texas",
                                "country": "United States",
                                "country_code": "US",
                                "latitude": 33.0,
                                "longitude": -96.0,
                            },
                        ]
                    }

            return {
                "current": {
                    "temperature_2m": 8.0,
                    "apparent_temperature": 6.0,
                    "relative_humidity_2m": 55,
                    "precipitation": 0.0,
                    "weather_code": 3,
                    "wind_speed_10m": 12.0,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        service = WeatherService(fetch_json=fake_fetch)

        result = await service.get_weather_text("New York, New York")

        self.assertIn("Weather for `New York, United States`", result)

    async def test_weather_service_matches_international_region_and_country_after_fallback(self) -> None:
        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            if "geocoding-api" in url:
                name = params["name"]
                if name == "Mainz, Rheinland-Pfalz, Germany":
                    return {"generationtime_ms": 1.0}
                if name == "Mainz":
                    return {
                        "results": [
                            {
                                "name": "Mainz",
                                "admin1": "Rheinland-Pfalz",
                                "country": "Germany",
                                "country_code": "DE",
                                "feature_code": "PPLA",
                                "population": 217556,
                                "latitude": 49.9842,
                                "longitude": 8.2791,
                            },
                            {
                                "name": "Mainz",
                                "admin1": "Bavaria",
                                "country": "Germany",
                                "country_code": "DE",
                                "feature_code": "PPL",
                                "population": 2000,
                                "latitude": 48.1351,
                                "longitude": 11.5820,
                            },
                        ]
                    }

            return {
                "current": {
                    "temperature_2m": 14.0,
                    "apparent_temperature": 13.0,
                    "relative_humidity_2m": 65,
                    "precipitation": 0.0,
                    "weather_code": 1,
                    "wind_speed_10m": 9.0,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        service = WeatherService(fetch_json=fake_fetch)

        result = await service.get_weather_text("Mainz, Rheinland-Pfalz, Germany")

        self.assertIn("Weather for `Mainz, Rheinland-Pfalz, Germany`", result)

    async def test_weather_service_matches_country_code_after_fallback(self) -> None:
        async def fake_fetch(url: str, params: dict[str, object]) -> dict[str, object]:
            if "geocoding-api" in url:
                name = params["name"]
                if name == "Wiesbaden, DE":
                    return {"generationtime_ms": 1.0}
                if name == "Wiesbaden":
                    return {
                        "results": [
                            {
                                "name": "Wiesbaden",
                                "admin1": "Hesse",
                                "country": "Germany",
                                "country_code": "DE",
                                "feature_code": "PPLA",
                                "population": 278342,
                                "latitude": 50.0826,
                                "longitude": 8.24,
                            }
                        ]
                    }

            return {
                "current": {
                    "temperature_2m": 12.0,
                    "apparent_temperature": 11.0,
                    "relative_humidity_2m": 70,
                    "precipitation": 0.4,
                    "weather_code": 61,
                    "wind_speed_10m": 11.0,
                    "time": "2026-03-12T19:00",
                },
                "current_units": {
                    "temperature_2m": "C",
                    "apparent_temperature": "C",
                    "relative_humidity_2m": "%",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
            }

        service = WeatherService(fetch_json=fake_fetch)

        result = await service.get_weather_text("Wiesbaden, DE")

        self.assertIn("Weather for `Wiesbaden, Hesse, Germany`", result)

    async def test_weather_service_requires_city(self) -> None:
        async def fake_fetch(_url: str, _params: dict[str, object]) -> dict[str, object]:
            return {}

        service = WeatherService(fetch_json=fake_fetch)

        result = await service.get_weather_text(None)

        self.assertEqual(result, "Usage: `weather <city>`")


class MicrosoftLearnMcpServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_msft_docs_formats_structured_results(self) -> None:
        async def fake_query(_query: str):
            return {
                "results": [
                    {
                        "title": "Authenticate with managed identity",
                        "url": "https://learn.microsoft.com/example/auth",
                        "content": "Use DefaultAzureCredential with a managed identity in Azure-hosted workloads.",
                    },
                    {
                        "title": "Azure Identity overview",
                        "url": "https://learn.microsoft.com/example/identity",
                        "content": "Understand credential chains and environment-specific authentication flows.",
                    },
                ]
            }

        service = MicrosoftLearnMcpService(query_tool=fake_query)

        result = await service.get_docs_text("managed identity")

        self.assertIn("Microsoft Learn results for `managed identity`", result)
        self.assertIn("Authenticate with managed identity", result)
        self.assertIn("https://learn.microsoft.com/example/auth", result)

    async def test_msft_docs_requires_query(self) -> None:
        service = MicrosoftLearnMcpService(query_tool=lambda _query: None)

        result = await service.get_docs_text("   ")

        self.assertEqual(result, "Usage: `msft_docs <question>`")


if __name__ == "__main__":
    unittest.main()