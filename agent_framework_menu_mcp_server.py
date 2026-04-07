from typing import Annotated

import anyio
from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import AzureCliCredential


@tool(approval_mode="never_require")
def get_specials() -> Annotated[str, "Returns the specials from the menu."]:
    return "Special Soup: Clam Chowder\nSpecial Salad: Cobb Salad\nSpecial Drink: Chai Tea"


@tool(approval_mode="never_require")
def get_item_price(
    menu_item: Annotated[str, "The name of the menu item."]
) -> Annotated[str, "Returns the price of the menu item."]:
    return "$9.99"


async def run() -> None:
    credential = AzureCliCredential()
    agent = Agent(
        client=OpenAIChatClient(
            model='gpt-5.4',
            azure_endpoint='https://zolabai-foundry-u49gco.cognitiveservices.azure.com/',
            credential=credential,
        ),
        name="RestaurantAgent",
        description="Answer questions about the menu.",
        instructions="You are a menu assistant. Answer briefly and use tools when needed.",
        tools=[get_specials, get_item_price],
    )

    server = agent.as_mcp_server()

    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(run)
