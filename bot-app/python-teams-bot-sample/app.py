from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.schema import Activity

from bot import FoundryTeamsBot
from conversation_store import JsonConversationStore
from job_dispatcher import FileJobDispatcher


BASE_PATH = Path(__file__).resolve().parent
QUEUE_PATH = BASE_PATH / ".queue"
STORE_PATH = BASE_PATH / ".state" / "conversations.json"

APP_ID = os.getenv("MicrosoftAppId", "")
APP_PASSWORD = os.getenv("MicrosoftAppPassword", "")
PORT = int(os.getenv("PORT", "3978"))

settings = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
adapter = BotFrameworkAdapter(settings)
dispatcher = FileJobDispatcher(QUEUE_PATH)
store = JsonConversationStore(STORE_PATH)
bot = FoundryTeamsBot(dispatcher=dispatcher, store=store)


async def on_error(turn_context: TurnContext, error: Exception):
    await turn_context.send_activity(f"Bot error: {error}")


adapter.on_turn_error = on_error


async def messages(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")
    response = await adapter.process_activity(activity, auth_header, bot.on_turn)
    if response:
        return web.json_response(data=response.body, status=response.status)
    return web.Response(status=201)


app = web.Application(middlewares=[aiohttp_error_middleware])
app.router.add_post("/api/messages", messages)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
