from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from auth_retry import authorize_with_retry, is_transient_jwt_authorization_error

try:
    from jwt.exceptions import PyJWKClientConnectionError
except Exception:  # pragma: no cover - fallback for minimal test environments
    class PyJWKClientConnectionError(Exception):
        pass


class AuthRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorize_with_retry_recovers_after_transient_jwks_failure(self) -> None:
        request = SimpleNamespace(method="POST", path="/api/messages")
        handler = object()
        logger = MagicMock()

        attempts = {"count": 0}

        async def flaky_authorize(_request, _handler):
            attempts["count"] += 1
            if attempts["count"] == 1:
                root = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
                raise PyJWKClientConnectionError("Fail to fetch data from the url") from root
            return "authorized"

        with patch("auth_retry.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await authorize_with_retry(
                request,
                handler,
                authorize=flaky_authorize,
                logger=logger,
                attempts=3,
                delay_seconds=0.01,
            )

        self.assertEqual(result, "authorized")
        self.assertEqual(attempts["count"], 2)
        logger.warning.assert_called_once()
        sleep_mock.assert_awaited_once()

    async def test_authorize_with_retry_does_not_retry_non_transient_error(self) -> None:
        request = SimpleNamespace(method="POST", path="/api/messages")
        logger = MagicMock()

        async def failing_authorize(_request, _handler):
            raise ValueError("bad token")

        with patch("auth_retry.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with self.assertRaisesRegex(ValueError, "bad token"):
                await authorize_with_retry(
                    request,
                    object(),
                    authorize=failing_authorize,
                    logger=logger,
                    attempts=3,
                    delay_seconds=0.01,
                )

        logger.warning.assert_not_called()
        sleep_mock.assert_not_awaited()

    def test_transient_jwt_error_detection_checks_exception_chain(self) -> None:
        root = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        exc = PyJWKClientConnectionError("Fail to fetch data from the url")
        exc.__cause__ = root

        self.assertTrue(is_transient_jwt_authorization_error(exc))
        self.assertFalse(is_transient_jwt_authorization_error(ValueError("bad token")))


if __name__ == "__main__":
    unittest.main()