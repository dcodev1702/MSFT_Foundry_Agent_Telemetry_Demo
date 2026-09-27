#!/usr/bin/env python3
"""Create or reuse the LiteLLM team, internal user and virtual key that the notebook calls the gateway with.

LiteLLM reports the key's alias, user, email and team as metadata.user_api_key_* on its spans. Budgets
and rate limits are not set. Organizations are a LiteLLM Enterprise feature, so none is created.
"""

import argparse
import json
from pathlib import Path
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
    def __init__(self, base_url: str, master_key: str, opener: Callable[..., Any] = urllib.request.urlopen):
        self.base_url = base_url.rstrip("/")
        self._master_key = master_key
        self._opener = opener

    def call(self, method: str, path: str, body: dict | None = None, *, missing_ok: bool = False) -> dict | None:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self._master_key}", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if missing_ok and error.code in {400, 404}:
                return None
            detail = error.read().decode(errors="replace")[:300]
            raise RuntimeError(f"LiteLLM {method} {path.split('?')[0]} failed with HTTP {error.code}: {detail}") from error


def _team_ids(user: dict) -> set[str]:
    info = user.get("user_info") or {}
    teams = set(info.get("teams") or [])
    teams.update(team.get("team_id") for team in user.get("teams") or [] if isinstance(team, dict))
    return teams


def ensure_identity(
    admin: LiteLLMAdmin, *, user_id: str, user_email: str | None, current_key: str | None,
) -> tuple[str, bool]:
    """Return the notebook's virtual key and whether it was newly generated."""
    if not admin.call("GET", f"/team/info?team_id={quote(TEAM_ID)}", missing_ok=True):
        admin.call("POST", "/team/new", {
            "team_id": TEAM_ID, "team_alias": TEAM_ALIAS,
            "metadata": {"purpose": "Microsoft Foundry agent telemetry demo"},
        })
    user = admin.call("GET", f"/user/info?user_id={quote(user_id)}", missing_ok=True) or {}
    if not user.get("user_info"):
        body = {"user_id": user_id, "user_role": "internal_user", "auto_create_key": False, "teams": [TEAM_ID]}
        if user_email:
            body["user_email"] = user_email
        admin.call("POST", "/user/new", body)
    elif TEAM_ID not in _team_ids(user):
        admin.call("POST", "/team/member_add", {"team_id": TEAM_ID, "member": {"role": "user", "user_id": user_id}})

    if current_key:
        info = (admin.call("GET", f"/key/info?key={quote(current_key)}", missing_ok=True) or {}).get("info") or {}
        allowed = (info.get("metadata") or {}).get("allowed_passthrough_routes")
        if (info.get("key_alias"), info.get("user_id"), info.get("team_id"), allowed) == (
            KEY_ALIAS, user_id, TEAM_ID, AGENT_ROUTES,
        ):
            return current_key, False
    admin.call("POST", "/key/delete", {"key_aliases": [KEY_ALIAS]}, missing_ok=True)
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
