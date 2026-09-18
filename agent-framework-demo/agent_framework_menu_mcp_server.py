import os
import sys
from typing import Annotated

import anyio
from agent_framework import Agent, tool
from agent_framework.observability import configure_otel_providers
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import AzureCliCredential

AGENT_SPEC_REVISION = '6c75f979a163'
CAPTURE_PROMPT_CONTENT = True
MESSAGE_EVENTS_ENABLED = False
SERVICE_VERSION = '2026.09.17'

@tool(approval_mode="never_require")
def get_specials() -> Annotated[str, "Returns the specials from the menu."]:
    return 'Special Soup: Clam Chowder\nSpecial Salad: Cobb Salad\nSpecial Drink: Chai Tea'

@tool(approval_mode="never_require")
def get_item_price(
    menu_item: Annotated[str, "The name of the menu item."]
) -> Annotated[str, "Returns the price of the menu item."]:
    return "$9.99"

async def run() -> None:
    azure_openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    model_name = (
        os.environ.get("AZURE_OPENAI_CHAT_MODEL", "").strip()
        or os.environ.get("AZURE_OPENAI_MODEL", "").strip()
    )
    if not azure_openai_endpoint or not model_name:
        raise RuntimeError(
            "Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_CHAT_MODEL "
            "(or AZURE_OPENAI_MODEL) before starting the MCP server."
        )

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    configure_otel_providers(
        service_name="zolab-agent-framework-mcp-demo",
        service_version=SERVICE_VERSION,
        resource_attributes={
            "service.namespace": "zolab-agent-framework",
            "service.instance.id": os.environ.get("OTEL_SERVICE_INSTANCE_ID", "mcp-stdio"),
            "deployment.environment.name": "demo",
            "demo.type": "agent-framework-mcp",
            "demo.agent.revision": AGENT_SPEC_REVISION,
        },
        otlp_endpoint=otlp_endpoint or None,
        otlp_protocol="grpc" if otlp_endpoint else None,
        enable_sensitive_data=CAPTURE_PROMPT_CONTENT,
        enable_console_exporters=False,
        enable_message_events=MESSAGE_EVENTS_ENABLED,
        otel_semconv_stability_opt_in="gen_ai_latest_experimental",
    )
    credential = AzureCliCredential()
    agent = Agent(
        client=OpenAIChatClient(
            model=model_name,
            azure_endpoint=azure_openai_endpoint,
            credential=credential,
        ),
        name='RestaurantAgent',
        description="Answer questions about the menu.",
        instructions='You are a menu assistant. Answer briefly and use tools when needed.',
        tools=[get_specials, get_item_price],
    )

    print(f"Starting MCP agent revision: {AGENT_SPEC_REVISION}", file=sys.stderr)
    if not otlp_endpoint:
        print("MCP telemetry has no OTLP destination; stdout remains reserved for the protocol.", file=sys.stderr)
    server = agent.as_mcp_server()

    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    anyio.run(run)
