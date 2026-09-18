# =============================================================================
# Author: dcodev1702 (with GitHub Copilot assistance)
# Updated: 2026-09-18
# Purpose: Host the notebook's ReviewerAgent in a separate, authenticated A2A
#          process while ArchitectAgent and CoachAgent remain local.
# Usage: Launched by agent_framework_reviewer_a2a_client.py with the demo .venv.
# Configuration: AGENT_DEMO_REVIEWER_SPEC, AGENT_DEMO_A2A_TOKEN,
#                AZURE_OPENAI_ENDPOINT, and the notebook's OTLP settings.
# Boundary: Loopback HTTP and one ephemeral bearer credential are for this local
#           demo only. Tasks are in memory and disappear when the process stops.
# =============================================================================

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass

import uvicorn
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder, StarletteUser
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils.constants import VERSION_HEADER
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from agent_framework import Agent, SupportsAgentRun
from agent_framework.a2a import A2AExecutor
from agent_framework.observability import configure_otel_providers
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import AzureCliCredential
from opentelemetry import propagate, trace
from starlette.applications import Starlette
from starlette.authentication import SimpleUser
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_framework_menu_mcp_server import finish_telemetry

SERVICE_NAME = "zolab-agent-framework-a2a-reviewer"
CARD_PATH = "/.well-known/agent-card.json"
REVIEW_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class ReviewerSpec:
    name: str
    description: str
    instructions: str
    model: str

    def __post_init__(self) -> None:
        if self.name != "ReviewerAgent":
            raise ValueError("The A2A service must host ReviewerAgent.")
        if not all(
            isinstance(value, str) and value.strip() for value in asdict(self).values()
        ):
            raise ValueError(
                "Reviewer name, description, instructions and model are required."
            )

    @property
    def revision(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class DemoCallContextBuilder(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        # Do not copy Authorization or other request headers into task state.
        return ServerCallContext(
            user=StarletteUser(request.user),
            state={
                "headers": {VERSION_HEADER: request.headers.get(VERSION_HEADER, "")}
            },
        )


class AuthenticatedTracingMiddleware:
    def __init__(
        self, app: ASGIApp, token: str, revision: str, export_enabled: bool
    ) -> None:
        if len(token) < 32 or not token.isascii():
            raise ValueError(
                "The A2A bearer token must be at least 32 ASCII characters."
            )
        self.app = app
        self.authorization = f"Bearer {token}".encode("ascii")
        self.revision = revision
        self.export_enabled = export_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        authorization = [
            value for key, value in headers if key.lower() == b"authorization"
        ]
        public_card = scope["method"] == "GET" and scope["path"] == CARD_PATH
        authorized = len(authorization) == 1 and hmac.compare_digest(
            authorization[0], self.authorization
        )
        carrier = {
            key.decode("ascii").lower(): value.decode("latin-1")
            for key, value in headers
            if key.lower() in (b"traceparent", b"tracestate")
        }
        tracer = trace.get_tracer("zolab.agent_framework_sdk.a2a")
        with tracer.start_as_current_span(
            "a2a.http.request",
            context=propagate.extract(carrier),
            kind=trace.SpanKind.SERVER,
            attributes={
                "http.request.method": scope["method"],
                "url.path": scope["path"],
                "demo.agent.revision": self.revision,
                "demo.a2a.authenticated": authorized,
            },
        ) as span:

            async def record_status(message):
                if message["type"] == "http.response.start":
                    status = message["status"]
                    span.set_attribute("http.response.status_code", status)
                    if status >= 400:
                        span.set_status(trace.StatusCode.ERROR, f"HTTP {status}")
                await send(message)

            if not public_card and not authorized:
                await JSONResponse(
                    {"error": "A valid demo bearer token is required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, record_status)
            else:
                scope["user"] = SimpleUser("notebook")
                await self.app(scope, receive, record_status)
        if self.export_enabled:
            await asyncio.to_thread(finish_telemetry)


class BoundedReviewerExecutor(A2AExecutor):
    async def execute(self, context, event_queue) -> None:
        # Cancellation is translated to a terminal canceled task by A2AExecutor.
        async with asyncio.timeout(REVIEW_TIMEOUT_SECONDS):
            await super().execute(context, event_queue)


def reviewer_card(spec: ReviewerSpec, base_url: str) -> AgentCard:
    return AgentCard(
        name=spec.name,
        description=spec.description,
        version=spec.revision,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=base_url, protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        security_schemes={
            "demoBearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="opaque",
                    description="Ephemeral local-demo token supplied by the notebook.",
                )
            )
        },
        security_requirements=[
            SecurityRequirement(schemes={"demoBearer": StringList()})
        ],
        skills=[
            AgentSkill(
                id="review-demo-runbook",
                name="Review a demo runbook",
                description=spec.description,
                tags=["review", "observability", "architecture"],
            )
        ],
    )


def create_app(
    agent: SupportsAgentRun,
    spec: ReviewerSpec,
    token: str,
    base_url: str,
    stop_server: Callable[[], None],
    *,
    export_enabled: bool = False,
) -> Starlette:
    card = reviewer_card(spec, base_url)
    handler = DefaultRequestHandler(
        agent_executor=BoundedReviewerExecutor(agent, stream=True),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    async def shutdown(request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "stopping"}, background=BackgroundTask(stop_server)
        )

    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(
                handler, "/", context_builder=DemoCallContextBuilder()
            ),
            Route("/shutdown", shutdown, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                AuthenticatedTracingMiddleware,
                token=token,
                revision=spec.revision,
                export_enabled=export_enabled,
            )
        ],
    )


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment setting: {name}")
    return value


def boolean_environment(name: str, default: bool) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false.")
    return value == "true"


async def main() -> None:
    spec = ReviewerSpec(**json.loads(required_environment("AGENT_DEMO_REVIEWER_SPEC")))
    token = required_environment("AGENT_DEMO_A2A_TOKEN")
    endpoint = required_environment("AZURE_OPENAI_ENDPOINT")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    configure_otel_providers(
        service_name=SERVICE_NAME,
        service_version=os.environ.get("OTEL_SERVICE_VERSION", "2026.09.18"),
        resource_attributes={
            "service.namespace": "zolab-agent-framework",
            "service.instance.id": required_environment("OTEL_SERVICE_INSTANCE_ID"),
            "demo.type": "agent-framework-a2a",
            "demo.agent.revision": spec.revision,
            "deployment.environment.name": "demo",
        },
        otlp_endpoint=otlp_endpoint or None,
        otlp_protocol="grpc" if otlp_endpoint else None,
        enable_sensitive_data=boolean_environment("AGENT_DEMO_CAPTURE_CONTENT", True),
        enable_message_events=boolean_environment("AGENT_DEMO_MESSAGE_EVENTS", False),
        enable_console_exporters=False,
        otel_semconv_stability_opt_in="gen_ai_latest_experimental",
    )
    logging.getLogger().setLevel(logging.WARNING)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}/"
        async with AzureCliCredential() as credential:
            try:
                agent = Agent(
                    client=OpenAIChatClient(
                        model=spec.model, azure_endpoint=endpoint, credential=credential
                    ),
                    name=spec.name,
                    description=spec.description,
                    instructions=spec.instructions,
                )

                def stop_server() -> None:
                    server.should_exit = True

                app = create_app(
                    agent,
                    spec,
                    token,
                    base_url,
                    stop_server,
                    export_enabled=bool(otlp_endpoint),
                )
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        log_level="warning",
                        access_log=False,
                        proxy_headers=False,
                        timeout_graceful_shutdown=10,
                    )
                )
                print(
                    json.dumps(
                        {
                            "url": base_url,
                            "pid": os.getpid(),
                            "revision": spec.revision,
                        }
                    ),
                    flush=True,
                )
                await server.serve(sockets=[listener])
            finally:
                if otlp_endpoint:
                    finish_telemetry(shutdown=True)


if __name__ == "__main__":
    asyncio.run(main())
