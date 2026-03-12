from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


SAMPLE_DIR = Path(__file__).resolve().parents[1]
if str(SAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(SAMPLE_DIR))

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

    async def test_weather_service_requires_city(self) -> None:
        service = WeatherService(fetch_json=lambda *_args, **_kwargs: None)

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