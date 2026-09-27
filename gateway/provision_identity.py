#!/usr/bin/env python3
"""Create or reuse the LiteLLM team, internal user and virtual key that the notebook calls the gateway with.

LiteLLM reports the key's alias, user, email and team as metadata.user_api_key_* on its spans. Budgets
and rate limits are not set. Organizations are a LiteLLM Enterprise feature, so none is created.
"""

import argparse
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable
import urllib.error
from urllib.parse import quote
import urllib.request

from refresh_runtime_env import DEFAULT_ENV_FILE, parse_env, write_env

TEAM_ID = "foundry-agent-demo"
TEAM_ALIAS = "Foundry Agent Demo"
KEY_ALIAS = "zolab-notebook-linux"
# Virtual keys may call auth=true pass-through routes only when the routes are allowed explicitly.
AGENT_ROUTES = ["/foundry-agent/main", "/foundry-agent/sentinel"]


class LiteLLMAdmin:
    def __init__(
        self, base_url: str, master_key: str, opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self._master_key = master_key
        self._opener = opener
        self._sleep = sleep

    def call(self, method: str, path: str, body: dict | None = None, *, attempts: int = 3) -> Any:
        """Send one admin request. Lookups (GET) are retried when Neon or LiteLLM briefly fails."""
        route = path.split("?")[0]
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                f"{self.base_url}{path}",
                data=None if body is None else json.dumps(body).encode(),
                method=method,
                headers={"Authorization": f"Bearer {self._master_key}", "Content-Type": "application/json"},
            )
            retry = method == "GET" and attempt < attempts
            try:
                with self._opener(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if retry and error.code >= 500:
                    self._sleep(attempt)
                    continue
                detail = error.read().decode(errors="replace")[:300]
                raise RuntimeError(f"LiteLLM {method} {route} failed with HTTP {error.code}: {detail}") from error
            except urllib.error.URLError as error:
                if retry:
                    self._sleep(attempt)
                    continue
                raise RuntimeError(f"LiteLLM {method} {route} is unreachable: {error.reason}") from error
        raise AssertionError("unreachable")


def ensure_identity(
    admin: LiteLLMAdmin, *, user_id: str, user_email: str | None, current_key: str | None,
) -> tuple[str, bool]:
    """Return the notebook's virtual key and whether it was newly generated.

    Lookups use LiteLLM's list endpoints, which return empty results rather than 404, so a first
    run records no failed requests in Application Insights.
    """
    if not any(team.get("team_id") == TEAM_ID for team in admin.call("GET", "/team/list") or []):
        admin.call("POST", "/team/new", {
            "team_id": TEAM_ID, "team_alias": TEAM_ALIAS,
            "metadata": {"purpose": "Microsoft Foundry agent telemetry demo"},
        })
    users = (admin.call("GET", f"/user/list?user_ids={quote(user_id)}") or {}).get("users") or []
    user = next((candidate for candidate in users if candidate.get("user_id") == user_id), None)
    if user is None:
        body = {"user_id": user_id, "user_role": "internal_user", "auto_create_key": False, "teams": [TEAM_ID]}
        if user_email:
            body["user_email"] = user_email
        admin.call("POST", "/user/new", body)
    elif TEAM_ID not in (user.get("teams") or []):
        admin.call("POST", "/team/member_add", {"team_id": TEAM_ID, "member": {"role": "user", "user_id": user_id}})

    listed = admin.call("GET", f"/key/list?key_alias={quote(KEY_ALIAS)}&return_full_object=true&size=100") or {}
    keys = [key for key in listed.get("keys") or [] if key.get("key_alias") == KEY_ALIAS]
    if current_key:
        # LiteLLM stores keys as SHA-256 hashes.
        current_hash = hashlib.sha256(current_key.encode()).hexdigest()
        for key in keys:
            allowed = (key.get("metadata") or {}).get("allowed_passthrough_routes")
            if (key.get("token"), key.get("user_id"), key.get("team_id"), allowed) == (
                current_hash, user_id, TEAM_ID, AGENT_ROUTES,
            ):
                return current_key, False
    if keys:
        admin.call("POST", "/key/delete", {"key_aliases": [KEY_ALIAS]})
    created = admin.call("POST", "/key/generate", {
        "key_alias": KEY_ALIAS, "user_id": user_id, "team_id": TEAM_ID,
        "metadata": {"allowed_passthrough_routes": AGENT_ROUTES, "notebook": "zolab-ai-agent-demo-linux.ipynb"},
    })
    if not created or not str(created.get("key", "")).startswith("sk-"):
        raise RuntimeError("LiteLLM did not return a virtual key.")
    return created["key"], True


def gateway_url(values: dict[str, str]) -> str:
    host = values.get("LITELLM_BIND_ADDRESS") or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{values.get('LITELLM_PORT') or '4000'}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    args = parser.parse_args()
    env_path = Path(args.env_file).expanduser().resolve()
    values = parse_env(env_path)
    user_id = values.get("LITELLM_USER_ID") or "zolab-notebook"
    admin = LiteLLMAdmin(gateway_url(values), values["LITELLM_MASTER_KEY"])
    key, created = ensure_identity(
        admin, user_id=user_id, user_email=values.get("LITELLM_USER_EMAIL") or None,
        current_key=values.get("LITELLM_NOTEBOOK_KEY"),
    )
    if created:
        write_env(env_path, {**values, "LITELLM_NOTEBOOK_KEY": key})
    print(f"LiteLLM identity: key {KEY_ALIAS} for {user_id} in team {TEAM_ALIAS} ({'created' if created else 'reused'})")


if __name__ == "__main__":
    main()
