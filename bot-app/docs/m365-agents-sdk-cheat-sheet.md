# M365 Agents SDK Cheat Sheet

This is the one-page version of the SDK surface used by this repo's Teams bot runtime.

## Import Map

| Import | What it gives you | Where it matters |
|---|---|---|
| `microsoft_agents.activity` | Activity models, env config loading, conversation references | Bootstrap and proactive messaging |
| `microsoft_agents.authentication.msal` | MSAL-based connection manager | Bot auth setup |
| `microsoft_agents.hosting.aiohttp` | Adapter, JWT middleware, request pipeline | HTTP hosting |
| `microsoft_agents.hosting.core` | Agent app model, authorization, storage, turn context, message helpers | Main bot logic |
| `microsoft_agents.hosting.teams` | Teams-specific helpers | Member lookup |
| `microsoft_agents.hosting.core.authorization` | Claims identity for proactive flows | Outbound continuation |

## Runtime Bootstrap

```python
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import AgentApplication, Authorization, MemoryStorage, TurnState

agents_sdk_config = load_configuration_from_env(environ)
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)

storage = MemoryStorage()
authorization = Authorization(storage, connection_manager, **agents_sdk_config)
agent_app = AgentApplication[TurnState](storage=storage, authorization=authorization)
```

## Incoming Messages

```python
from microsoft_agents.hosting.core import MessageFactory, TurnContext, TurnState

@agent_app.activity("message")
async def on_message(context: TurnContext, state: TurnState):
    await context.send_activity(MessageFactory.text("Hello from the bot"))
```

## Teams-Specific Lookup

```python
from microsoft_agents.hosting.teams import TeamsInfo

member = await TeamsInfo.get_member(context, member_id)
```

## Proactive Messaging

```python
from microsoft_agents.activity import Activity, ConversationReference
from microsoft_agents.hosting.core.authorization import ClaimsIdentity

conversation_reference = ConversationReference().deserialize(reference_data)
typing_activity = Activity(type="typing")
claims_identity = ClaimsIdentity(claims={"aud": app_id, "appid": app_id}, authentication_type="proactive", is_authenticated=True)
```

## Mental Model

- `load_configuration_from_env`: Pull SDK-required settings from environment variables.
- `MsalConnectionManager`: Authenticate the bot with Microsoft identity infrastructure.
- `CloudAdapter`: Bridge HTTP requests to bot activities.
- `AgentApplication`: Register handlers and hold app-level bot behavior.
- `TurnContext`: Read the incoming activity and send replies.
- `MessageFactory`: Build outbound text messages.
- `TeamsInfo`: Ask Teams for member metadata.
- `ConversationReference`: Save enough context to message later.
- `ClaimsIdentity`: Supply identity context for proactive continuation.

## Repo Flow In One Line

Environment config -> MSAL auth -> adapter -> agent app -> message handlers -> Teams helpers -> proactive continuation.