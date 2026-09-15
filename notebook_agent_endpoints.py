"""Explicit project/agent-endpoint routing for the Windows notebook."""

from collections.abc import Mapping
from dataclasses import dataclass
import os
import re
from urllib.parse import urljoin, urlsplit

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentDefinition, AgentVersionDetails
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
        return cls(mode="agent_endpoint", main=targets["main"], sentinel=targets["sentinel"])


def get_pinned_agent(
    client: AIProjectClient, target: AgentTarget, expected_definition: AgentDefinition,
    *, span: Span | None = None, **kwargs,
) -> AgentVersionDetails:
    """Read and validate a release; never create versions or change traffic routing."""
    details = client.agents.get(agent_name=target.name, **kwargs)
    if details.state != "enabled":
        raise RuntimeError(f"{target.name} is not enabled for backend requests.")
    identity = details.instance_identity
    if identity is None or not identity.principal_id or not identity.client_id:
        raise RuntimeError(f"{target.name} has no complete unique agent identity; endpoint cutover is blocked.")
    endpoint = details.agent_endpoint
    if endpoint is None:
        raise RuntimeError(f"{target.name} has no agent endpoint configuration.")
    configuration = endpoint.as_dict()
    rules = configuration.get("version_selector", {}).get("version_selection_rules", [])
    expected_rules = [{
        "type": "FixedRatio", "agent_version": target.version, "traffic_percentage": 100,
    }]
    if rules != expected_rules:
        raise RuntimeError(
            f"{target.name} is not pinned exclusively to version {target.version}. "
            "Promote a tested version explicitly; notebook reruns never change routing."
        )
    protocols = configuration.get("protocol_configuration", {})
    schemes = configuration.get("authorization_schemes", [])
    if "responses" not in protocols or not any(scheme.get("type") == "Entra" for scheme in schemes):
        raise RuntimeError(f"{target.name} must expose the Responses protocol with Entra authorization.")
    version = client.agents.get_version(agent_name=target.name, agent_version=target.version, **kwargs)
    if str(version.version) != target.version:
        raise RuntimeError(f"Unexpected version returned for {target.name}:{target.version}.")
    if version.definition.as_dict() != expected_definition.as_dict():
        raise RuntimeError(
            f"The notebook definition differs from pinned {target.name}:{target.version}. "
            "Create and test a candidate, then explicitly update the endpoint pin and local metadata."
        )
    if span is not None:
        span.set_attribute("app.agent.identity.principal_id", identity.principal_id)
        span.set_attribute("app.agent.identity.client_id", identity.client_id)
        span.set_attribute("app.agent.active_version", target.version)
    return version


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
