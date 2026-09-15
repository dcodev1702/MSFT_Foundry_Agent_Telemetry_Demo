"""Explicit project/agent-endpoint routing for the Windows notebook."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin, urlsplit

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentDefinition, AgentDetails, AgentEndpointConfig, AgentVersionDetails
from openai import OpenAI
from opentelemetry.trace import Span


@dataclass(frozen=True)
class AgentTarget:
    name: str
    version: str


@dataclass(frozen=True)
class AgentRuntimeConfig:
    mode: str
    main: AgentTarget | None = None
    sentinel: AgentTarget | None = None
    version_policy: str = "pinned"

    @property
    def uses_agent_endpoint(self) -> bool:
        return self.mode == "agent_endpoint"

    @property
    def label(self) -> str:
        return (
            "responses.create + agent endpoint"
            if self.uses_agent_endpoint else "responses.create + agent_reference"
        )

    def target(self, role: str) -> AgentTarget:
        if role not in {"main", "sentinel"}:
            raise ValueError(f"Unknown notebook agent role: {role!r}")
        target = self.main if role == "main" else self.sentinel
        if target is None:
            raise ValueError(f"No agent endpoint target configured for {role}.")
        return target

    @classmethod
    def from_build_info(
        cls, build_info: Mapping[str, object], environment: Mapping[str, str] | None = None,
    ) -> "AgentRuntimeConfig":
        environment = os.environ if environment is None else environment
        mode = environment.get(
            "FOUNDRY_AGENT_INVOCATION_MODE", build_info.get("agent_invocation_mode", "project"),
        )
        if not isinstance(mode, str) or mode not in {"project", "agent_endpoint"}:
            raise ValueError("agent_invocation_mode must be 'project' or 'agent_endpoint'.")
        if mode == "project":
            return cls(mode="project")
        policy = environment.get(
            "FOUNDRY_AGENT_VERSION_POLICY", build_info.get("backend_version_policy", "pinned"),
        )
        if not isinstance(policy, str) or policy not in {"pinned", "sync"}:
            raise ValueError("backend_version_policy must be 'pinned' or 'sync'.")
        configured = build_info.get("backend_agents")
        if not isinstance(configured, Mapping):
            raise ValueError("Agent endpoint mode requires backend_agents in the local build metadata.")
        targets = {}
        for role in ("main", "sentinel"):
            value = configured.get(role)
            if not isinstance(value, Mapping):
                raise ValueError(f"backend_agents.{role} must contain a name and pinned version.")
            name, version = value.get("name"), value.get("version")
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", name,
            ):
                raise ValueError(f"Invalid backend agent name for {role}.")
            if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*", version):
                raise ValueError(f"backend_agents.{role}.version must be a fixed positive version string.")
            targets[role] = AgentTarget(name=name, version=version)
        if targets["main"].name == targets["sentinel"].name:
            raise ValueError("Main and Sentinel must use distinct backend agents.")
        return cls(
            mode="agent_endpoint", main=targets["main"], sentinel=targets["sentinel"],
            version_policy=policy,
        )


def _validated_endpoint(details: AgentDetails, name: str) -> dict:
    if details.state != "enabled":
        raise RuntimeError(f"{name} is not enabled for backend requests.")
    identity = details.instance_identity
    if identity is None or not identity.principal_id or not identity.client_id:
        raise RuntimeError(f"{name} has no complete unique agent identity; endpoint cutover is blocked.")
    endpoint = details.agent_endpoint
    if endpoint is None:
        raise RuntimeError(f"{name} has no agent endpoint configuration.")
    configuration = deepcopy(endpoint.as_dict())
    protocols = configuration.get("protocol_configuration", {})
    schemes = configuration.get("authorization_schemes", [])
    if "responses" not in protocols or not any(scheme.get("type") == "Entra" for scheme in schemes):
        raise RuntimeError(f"{name} must expose the Responses protocol with Entra authorization.")
    return configuration


def _fixed_rules(version: str) -> list[dict]:
    return [{"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100}]


def _identity_snapshot(details: AgentDetails) -> dict:
    identity = details.instance_identity
    if identity is None:
        raise RuntimeError("Agent identity disappeared during synchronization.")
    return identity.as_dict()


def _record_agent_identity(span: Span | None, details: AgentDetails, version: str) -> None:
    if span is not None:
        identity = details.instance_identity
        if identity is None:
            raise RuntimeError("Cannot record an unverified agent identity.")
        span.set_attribute("app.agent.identity.principal_id", identity.principal_id)
        span.set_attribute("app.agent.identity.client_id", identity.client_id)
        span.set_attribute("app.agent.active_version", version)


def get_pinned_agent(
    client: AIProjectClient, target: AgentTarget, expected_definition: AgentDefinition,
    *, span: Span | None = None, **kwargs,
) -> AgentVersionDetails:
    """Read and validate a strict release without changing versions or routing."""
    details = client.agents.get(agent_name=target.name, **kwargs)
    configuration = _validated_endpoint(details, target.name)
    rules = configuration.get("version_selector", {}).get("version_selection_rules", [])
    if rules != _fixed_rules(target.version):
        raise RuntimeError(
            f"{target.name} is not pinned exclusively to version {target.version}. "
            "Promote a tested version explicitly; notebook reruns never change routing."
        )
    version = client.agents.get_version(agent_name=target.name, agent_version=target.version, **kwargs)
    if str(version.version) != target.version:
        raise RuntimeError(f"Unexpected version returned for {target.name}:{target.version}.")
    if version.definition.as_dict() != expected_definition.as_dict():
        raise RuntimeError(
            f"The notebook definition differs from pinned {target.name}:{target.version}. "
            "Create and test a candidate, then explicitly update the endpoint pin and local metadata."
        )
    _record_agent_identity(span, details, target.version)
    return version


def sync_agent_version(
    client: AIProjectClient, target: AgentTarget, expected_definition: AgentDefinition,
    *, span: Span | None = None, **kwargs,
) -> AgentVersionDetails:
    """Reconcile the notebook definition with a concrete active endpoint version."""
    details = client.agents.get(agent_name=target.name, **kwargs)
    configuration = _validated_endpoint(details, target.name)
    identity = _identity_snapshot(details)
    rules = configuration.get("version_selector", {}).get("version_selection_rules", [])
    active = rules[0].get("agent_version") if len(rules) == 1 else None
    if not isinstance(active, str) or not re.fullmatch(r"[1-9][0-9]*", active) or rules != _fixed_rules(active):
        raise RuntimeError(f"{target.name} must have a single fixed version before synchronization; split/latest routing is not overwritten.")
    expected = expected_definition.as_dict()
    selected = client.agents.get_version(agent_name=target.name, agent_version=active, **kwargs)
    if str(selected.version) != active:
        raise RuntimeError(f"Unexpected active version returned for {target.name}.")
    action = "reuse_active"
    if selected.definition.as_dict() != expected:
        latest = details.versions.latest
        if latest.definition.as_dict() == expected:
            selected = latest
            action = "reuse_latest"
        else:
            selected = client.agents.create_version(
                agent_name=target.name, definition=expected_definition, **kwargs,
            )
            action = "create_version"
    selected_version = str(selected.version)
    if not re.fullmatch(r"[1-9][0-9]*", selected_version) or selected.definition.as_dict() != expected:
        raise RuntimeError(f"Selected version for {target.name} does not match the notebook definition; endpoint not promoted.")
    if span is not None:
        span.set_attribute("app.agent.version_policy", "sync")
        span.set_attribute("app.agent.previous_version", active)
        span.set_attribute("app.agent.version_action", action)
    if selected_version != active:
        before_update = client.agents.get(agent_name=target.name, **kwargs)
        if (_validated_endpoint(before_update, target.name) != configuration
                or _identity_snapshot(before_update) != identity):
            raise RuntimeError(f"{target.name} changed during synchronization; endpoint not overwritten. Rerun after resolving concurrent changes.")
        client.agents.update_details(
            agent_name=target.name,
            agent_endpoint=AgentEndpointConfig({
                "version_selector": {"version_selection_rules": _fixed_rules(selected_version)},
            }),
            **kwargs,
        )
        if span is not None:
            span.add_event("agent_version.endpoint_updated", {"app.agent.active_version": selected_version})
    confirmed = client.agents.get(agent_name=target.name, **kwargs)
    confirmed_configuration = _validated_endpoint(confirmed, target.name)
    expected_configuration = {
        **configuration,
        "version_selector": {"version_selection_rules": _fixed_rules(selected_version)},
    }
    if (confirmed_configuration != expected_configuration
            or _identity_snapshot(confirmed) != identity):
        raise RuntimeError(f"Could not verify {target.name} on version {selected_version}; inspect endpoint state before retrying.")
    _record_agent_identity(span, confirmed, selected_version)
    return selected


def _save_selected_version(path: Path, original: bytes, data: dict) -> None:
    content = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if path.read_bytes() != original:
            raise RuntimeError("Build metadata changed during synchronization; local edits were not overwritten.")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_backend_agent(
    client: AIProjectClient, runtime: AgentRuntimeConfig, role: str, expected_definition: AgentDefinition,
    *, build_info: dict, build_info_path: Path, span: Span | None = None, **kwargs,
) -> tuple[AgentVersionDetails, AgentRuntimeConfig]:
    target = runtime.target(role)
    if runtime.version_policy == "pinned":
        if span is not None:
            span.set_attribute("app.agent.version_policy", "pinned")
        return get_pinned_agent(client, target, expected_definition, span=span, **kwargs), runtime
    if runtime.version_policy != "sync":
        raise ValueError("Unsupported backend version policy.")
    path = Path(build_info_path)
    original = path.read_bytes()
    data = json.loads(original)
    if not isinstance(data, dict):
        raise ValueError("Build metadata must be a JSON object.")
    stored = AgentRuntimeConfig.from_build_info(data, {"FOUNDRY_AGENT_INVOCATION_MODE": "agent_endpoint"})
    if (stored.target(role).name != target.name
            or data.get("foundry_project_endpoint") != build_info.get("foundry_project_endpoint")):
        raise RuntimeError("Build metadata targets changed; rerun Section 3 before synchronizing.")
    selected = sync_agent_version(client, target, expected_definition, span=span, **kwargs)
    version = str(selected.version)
    if data["backend_agents"][role]["version"] != version:
        data["backend_agents"][role]["version"] = version
        try:
            _save_selected_version(path, original, data)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(
                f"{target.name} is active on version {version}, but its local selection could not be saved. "
                "Resolve the metadata error and rerun synchronization to reconcile it."
            ) from error
    elif path.read_bytes() != original:
        raise RuntimeError("Build metadata changed during synchronization; rerun Section 3.")
    build_info["backend_agents"] = deepcopy(data["backend_agents"])
    updated_runtime = replace(runtime, **{role: AgentTarget(name=target.name, version=version)})
    if span is not None:
        span.add_event("agent_version.selection_saved", {"app.agent.active_version": version})
    return selected, updated_runtime


def get_agent_openai_client(
    client: AIProjectClient, runtime: AgentRuntimeConfig, agent_name: str,
) -> OpenAI:
    if runtime.uses_agent_endpoint:
        if agent_name not in {runtime.target("main").name, runtime.target("sentinel").name}:
            raise ValueError("Agent is not one of the configured backend endpoints.")
        return client.get_openai_client(agent_name=agent_name)
    return client.get_openai_client()


def response_options(runtime: AgentRuntimeConfig, reference_payload: dict) -> dict:
    return {} if runtime.uses_agent_endpoint else {"extra_body": reference_payload}


def responses_url(client: OpenAI) -> str:
    base = str(client.base_url).rstrip("/") + "/"
    parsed = urlsplit(base)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("Responses client has no valid HTTP base URL.")
    if not parsed.path.endswith(("/openai/v1/", "/endpoint/protocols/openai/")):
        raise ValueError(f"Unsupported Responses client path: {parsed.path}")
    return urljoin(base, "responses")
