"""Optional local LiteLLM gateway for the notebook's Foundry agent traffic."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
import urllib.error
import urllib.request
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from notebook_support.agent_endpoints import AgentRuntimeConfig


GATEWAY_MODES = ("direct", "litellm")
GATEWAY_ROLES = ("main", "sentinel")
ROUTE_PREFIX = "/foundry-agent"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / "gateway" / ".env"
# Azure CLI can reuse a cached token until about five minutes before expiry.
MINIMUM_TOKEN_LIFETIME = timedelta(minutes=10)
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class GatewayConfig:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    token_expires_at: datetime | None = None
    env_file: Path | None = None

    def agent_base_url(self, role: str) -> str:
        if role not in GATEWAY_ROLES:
            raise ValueError(f"Unknown gateway route: {role!r}")
        return f"{self.base_url}{ROUTE_PREFIX}/{role}"

    def span_attributes(self, upstream_endpoint: str) -> dict[str, str]:
        return {
            "app.gateway.name": self.name,
            "app.upstream.server.address": urlsplit(upstream_endpoint).hostname or "",
        }


def _parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not _ENV_KEY.fullmatch(key):
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def gateway_mode(
    build_info: Mapping[str, object], environment: Mapping[str, str] | None = None,
) -> str:
    environment = os.environ if environment is None else environment
    mode = environment.get("FOUNDRY_AGENT_GATEWAY", build_info.get("agent_gateway", "direct"))
    if not isinstance(mode, str) or mode not in GATEWAY_MODES:
        raise ValueError("agent_gateway must be 'direct' or 'litellm'.")
    return mode


def load_gateway_config(environment: Mapping[str, str] | None = None) -> GatewayConfig:
    """Read the gateway's local settings without exposing the master key."""
    environment = os.environ if environment is None else environment
    env_file = Path(environment.get("FOUNDRY_AGENT_GATEWAY_ENV_FILE") or DEFAULT_ENV_FILE).expanduser()
    if not env_file.is_file():
        raise FileNotFoundError(
            f"LiteLLM gateway settings were not found at {env_file}. Run gateway/start.sh first."
        )
    values = _parse_env_file(env_file)
    api_key = values.get("LITELLM_MASTER_KEY", "")
    if not api_key.startswith("sk-") or "REPLACE_WITH" in api_key:
        raise ValueError(f"{env_file} has no generated LITELLM_MASTER_KEY. Run gateway/start.sh.")
    host = values.get("LITELLM_BIND_ADDRESS") or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    port = values.get("LITELLM_PORT") or "4000"
    if not port.isdigit():
        raise ValueError(f"{env_file} has an invalid LITELLM_PORT.")
    token_expires_at = None
    if expires := values.get("AZURE_AD_TOKEN_EXPIRES_ON"):
        try:
            token_expires_at = datetime.fromisoformat(expires)
        except ValueError as error:
            raise ValueError(f"{env_file} has an invalid AZURE_AD_TOKEN_EXPIRES_ON.") from error
        if token_expires_at.tzinfo is None:
            token_expires_at = token_expires_at.replace(tzinfo=timezone.utc)
    return GatewayConfig(
        name="litellm", base_url=f"http://{host}:{port}", api_key=api_key,
        token_expires_at=token_expires_at, env_file=env_file,
    )


def configure_agent_gateway(
    runtime: "AgentRuntimeConfig", build_info: Mapping[str, object],
    environment: Mapping[str, str] | None = None,
) -> "AgentRuntimeConfig":
    """Return the direct runtime unchanged, or bind it to the local LiteLLM gateway."""
    if gateway_mode(build_info, environment) == "direct":
        return runtime
    if not runtime.uses_agent_endpoint:
        raise ValueError(
            "The LiteLLM gateway routes Foundry agent endpoints; set agent_invocation_mode to 'agent_endpoint'."
        )
    return replace(runtime, gateway=load_gateway_config(environment))


def check_gateway_ready(
    gateway: GatewayConfig, *, minimum_token_lifetime: timedelta = MINIMUM_TOKEN_LIFETIME,
    now: datetime | None = None, opener: Callable[..., object] = urllib.request.urlopen,
) -> dict[str, object]:
    """Fail before agent calls when the gateway, its database, or its Foundry token is unusable."""
    now = datetime.now(timezone.utc) if now is None else now
    remaining = None if gateway.token_expires_at is None else gateway.token_expires_at - now
    if remaining is None or remaining < minimum_token_lifetime:
        raise RuntimeError(
            "The LiteLLM gateway's Foundry token expires too soon for a notebook run. "
            "Run gateway/start.sh in this host's terminal, then rerun this cell. Azure CLI can "
            "reuse its cached token until about five minutes before expiry; if the expiry does "
            "not change, wait and run gateway/start.sh again."
        )
    try:
        with opener(f"{gateway.base_url}/health/readiness", timeout=10) as response:
            readiness = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"The LiteLLM gateway is not ready (HTTP {error.code}). Run gateway/start.sh."
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"The LiteLLM gateway is unreachable at {gateway.base_url}. Run gateway/start.sh."
        ) from error
    if readiness.get("db") != "connected":
        raise RuntimeError(
            "The LiteLLM gateway reports that its Neon database is not connected. Run gateway/start.sh."
        )
    return {"db": readiness["db"], "token_minutes_remaining": int(remaining.total_seconds() // 60)}
