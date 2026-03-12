from __future__ import annotations


PUBLIC_EXACT_PATHS = {
    "/api/messages",
    "/bot-icon.png",
}


def is_anonymous_route(method: str, path: str) -> bool:
    normalized_method = (method or "").upper()
    if normalized_method not in {"GET", "HEAD"}:
        return False

    if path in PUBLIC_EXACT_PATHS:
        return True

    if path.startswith("/api/download/"):
        return True

    if path.startswith("/bot-icon-") and path.endswith(".png"):
        return True

    return False