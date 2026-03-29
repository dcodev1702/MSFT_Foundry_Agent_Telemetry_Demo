from __future__ import annotations

import asyncio
import os
import ssl
import urllib.error
from typing import Awaitable, Callable

try:
    from jwt.exceptions import PyJWKClientConnectionError
except Exception:  # pragma: no cover - fallback for minimal test environments
    class PyJWKClientConnectionError(Exception):
        pass


JWT_AUTH_RETRY_ATTEMPTS = max(1, int(os.getenv("JWT_AUTH_RETRY_ATTEMPTS", "3")))
JWT_AUTH_RETRY_DELAY_SECONDS = max(
    0.0,
    float(os.getenv("JWT_AUTH_RETRY_DELAY_SECONDS", "0.5")),
)


def _iter_exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient_jwt_authorization_error(exc: BaseException) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(current, PyJWKClientConnectionError):
            return True

        if isinstance(current, (urllib.error.URLError, ConnectionResetError, TimeoutError, ssl.SSLError)):
            return True

        if isinstance(current, OSError) and getattr(current, "errno", None) in {104, 110, 111}:
            return True

        message = str(current).lower()
        if "fail to fetch data from the url" in message:
            return True
        if "connection reset by peer" in message:
            return True

    return False


async def authorize_with_retry(
    request,
    handler,
    *,
    authorize: Callable[[object, object], Awaitable[object]],
    logger,
    attempts: int = JWT_AUTH_RETRY_ATTEMPTS,
    delay_seconds: float = JWT_AUTH_RETRY_DELAY_SECONDS,
):
    attempts = max(1, attempts)

    for attempt in range(1, attempts + 1):
        try:
            return await authorize(request, handler)
        except Exception as exc:
            if attempt >= attempts or not is_transient_jwt_authorization_error(exc):
                raise

            logger.warning(
                "Transient JWT authorization failure for %s %s; retrying in %.2fs (%d/%d): %s",
                getattr(request, "method", "<unknown>"),
                getattr(request, "path", "<unknown>"),
                delay_seconds,
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(delay_seconds * attempt)