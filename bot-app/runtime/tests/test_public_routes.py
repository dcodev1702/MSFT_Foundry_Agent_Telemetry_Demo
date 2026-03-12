from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from public_routes import is_anonymous_route


class PublicRouteTests(unittest.TestCase):
    def test_download_route_is_public_for_get(self) -> None:
        self.assertTrue(is_anonymous_route("GET", "/api/download/build_info-v9ib2m.json"))

    def test_versioned_bot_icon_is_public(self) -> None:
        self.assertTrue(is_anonymous_route("GET", "/bot-icon-20260312.png"))

    def test_post_messages_route_stays_protected(self) -> None:
        self.assertFalse(is_anonymous_route("POST", "/api/messages"))

    def test_unrelated_api_route_stays_protected(self) -> None:
        self.assertFalse(is_anonymous_route("GET", "/api/internal/status"))


if __name__ == "__main__":
    unittest.main()