"""Optional local LiteLLM gateway for the notebook's Foundry agent traffic."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
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
COMPOSE_PROJECT = "foundry-agent-gateway"
NEON_REGION_NAMES = {
    "us-east-1": "N. Virginia", "us-east-2": "Ohio", "us-west-2": "Oregon", "eu-central-1": "Frankfurt",
    "eu-west-2": "London", "ap-southeast-1": "Singapore", "ap-southeast-2": "Sydney", "sa-east-1": "São Paulo",
}
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_NEON_HOST = re.compile(r"^ep-[a-z0-9-]+(?:\.c-\d+)?\.(?P<region>[a-z]{2}-[a-z]+-\d)\.aws\.neon\.tech$")


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


def _neon_location(database_url: str) -> str:
    parsed = urlsplit(database_url)
    database = parsed.path.lstrip("/") or "database"
    match = _NEON_HOST.fullmatch(parsed.hostname or "")
    if not match:
        return f"{database} on a non-Neon host"
    region = match["region"]
    return f"{database} in aws-{region} ({NEON_REGION_NAMES.get(region, region)})"


def _collector_status(run: Callable[..., subprocess.CompletedProcess]) -> str:
    try:
        result = run(
            ["docker", "ps", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
             "--filter", "label=com.docker.compose.service=otel-collector", "--format", "{{.Image}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "⚠️ Docker status unavailable"
    if result.returncode != 0:
        return "⚠️ Docker status unavailable"
    lines = result.stdout.strip().splitlines()
    if not lines:
        return "❌ Not running; run gateway/start.sh"
    image, _, status = lines[0].partition("\t")
    name = image.split("@", 1)[0]
    version = name.rsplit(":", 1)[1] if ":" in name.rsplit("/", 1)[-1] else "latest"
    state = "✅" if status.startswith("Up") else "⚠️"
    return f"{state} otelcol-contrib {version}, {status} → App Insights"


def gateway_infrastructure_status(
    build_info: Mapping[str, object], environment: Mapping[str, str] | None = None, *,
    now: datetime | None = None, opener: Callable[..., object] = urllib.request.urlopen,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, str]:
    """Return deployment-table values for the "litellm", "otel_collector", and "neon" rows.

    Never raises for an unhealthy gateway and never includes keys, hostnames, or credentials.
    """
    try:
        mode = gateway_mode(build_info, environment)
    except ValueError as error:
        return {"litellm": f"⚠️ {error}", "otel_collector": "➖ Not checked", "neon": "➖ Not checked"}
    if mode == "direct":
        return dict.fromkeys(("litellm", "otel_collector", "neon"), "➖ Not used (agent_gateway=direct)")
    try:
        gateway = load_gateway_config(environment)
    except (OSError, ValueError):
        return {
            "litellm": "❌ Not configured; run gateway/start.sh",
            "otel_collector": "➖ Not checked", "neon": "➖ Not checked",
        }

    readiness: dict[str, object] = {}
    http_status: int | None = None
    try:
        with opener(f"{gateway.base_url}/health/readiness", timeout=5) as response:
            readiness = json.load(response)
    except urllib.error.HTTPError as error:
        http_status = error.code
        try:
            readiness = json.loads(error.read() or b"{}")
        except (OSError, ValueError):
            readiness = {}
    except OSError:
        http_status = -1

    now = datetime.now(timezone.utc) if now is None else now
    minutes = None if gateway.token_expires_at is None else int((gateway.token_expires_at - now).total_seconds() // 60)
    if http_status == -1:
        gateway_value = f"❌ Unreachable at {gateway.base_url}; run gateway/start.sh"
    elif http_status is not None:
        gateway_value = f"⚠️ Not ready at {gateway.base_url} (HTTP {http_status})"
    elif minutes is None or minutes < MINIMUM_TOKEN_LIFETIME.total_seconds() // 60:
        remaining = "unknown" if minutes is None else "expired" if minutes < 0 else f"{minutes} min left"
        gateway_value = f"⚠️ Ready at {gateway.base_url}, Foundry token {remaining}; run gateway/start.sh"
    else:
        gateway_value = f"✅ Ready at {gateway.base_url}, Foundry token valid {minutes} min"

    database_url = _parse_env_file(gateway.env_file).get("DATABASE_URL", "") if gateway.env_file else ""
    location = _neon_location(database_url) if database_url else "not configured"
    database_status = readiness.get("db") if http_status != -1 else None
    if database_status == "connected":
        neon_value = f"✅ Connected, {location}"
    elif database_status:
        neon_value = f"❌ {str(database_status).capitalize()}, {location}; check the Neon Console"
    else:
        neon_value = f"⚠️ Status unknown, {location}"
    return {"litellm": gateway_value, "otel_collector": _collector_status(run), "neon": neon_value}
