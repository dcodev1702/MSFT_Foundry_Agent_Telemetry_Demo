# M365 Agents SDK Imports Used In This Repo

The notebook itself does **not** appear to directly import the M365 Agents SDK right now. The SDK usage is in the **Teams bot runtime**.

## 1. `microsoft_agents.activity`

### What you use

- `load_configuration_from_env`
- `Activity`
- `ConversationReference`

### What it provides

- `load_configuration_from_env`: Loads the bot and agent settings the SDK expects from environment variables.
- `Activity`: The core Bot Framework and Teams activity payload model.
- `ConversationReference`: A serialized pointer to an existing conversation so you can send proactive messages later.

### Example

```python
from microsoft_agents.activity import load_configuration_from_env

agents_sdk_config = load_configuration_from_env(environ)
```

And in proactive messaging:

```python
from microsoft_agents.activity import Activity, ConversationReference

conversation_reference = ConversationReference().deserialize(reference_data)
typing_activity = Activity(type="typing")
```

## 2. `microsoft_agents.authentication.msal`

### What you use

- `MsalConnectionManager`

### What it provides

This sets up the SDK authentication layer using MSAL so the bot can authenticate correctly with Microsoft 365 and Bot channel infrastructure.

### Example

```python
from microsoft_agents.authentication.msal import MsalConnectionManager

connection_manager = MsalConnectionManager(**agents_sdk_config)
```

## 3. `microsoft_agents.hosting.aiohttp`

### What you use

- `CloudAdapter`
- `jwt_authorization_middleware`
- `start_agent_process`

### What it provides

- `CloudAdapter`: The main runtime adapter that sends and receives activities.
- `jwt_authorization_middleware`: Validates incoming requests.
- `start_agent_process`: Hands an incoming HTTP request to the SDK pipeline.

### Example

```python
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)

adapter = CloudAdapter(connection_manager=connection_manager)
app.middlewares.append(jwt_authorization_middleware)

async def messages(request: web.Request) -> web.StreamResponse:
    return await start_agent_process(request, agent_app, adapter)
```

## 4. `microsoft_agents.hosting.core`

### What you use

- `AgentApplication`
- `Authorization`
- `MemoryStorage`
- `TurnState`
- `TurnContext`
- `MessageFactory`

### What it provides

This is the core bot and agent programming model.

- `AgentApplication`: Your bot application object where handlers are registered.
- `Authorization`: Authorization support used by the SDK runtime.
- `MemoryStorage`: Simple in-memory state store.
- `TurnState`: Per-turn state object.
- `TurnContext`: The current inbound activity context.
- `MessageFactory`: Helper for building outgoing messages.

### Example: app bootstrap

```python
from microsoft_agents.hosting.core import AgentApplication, Authorization, MemoryStorage

storage = MemoryStorage()
authorization = Authorization(storage, connection_manager, **agents_sdk_config)

agent_app = AgentApplication[TurnState](
    storage=storage,
    authorization=authorization,
)
```

### Example: message handler

```python
from microsoft_agents.hosting.core import MessageFactory, TurnContext, TurnState

@agent_app.activity("message")
async def on_message(context: TurnContext, state: TurnState):
    await context.send_activity(MessageFactory.text("Hello from the bot"))
```

## 5. `microsoft_agents.hosting.teams`

### What you use

- `TeamsInfo`

### What it provides

Teams-specific helpers. In this repo, this is used to resolve user and member details from Teams.

### Example

```python
from microsoft_agents.hosting.teams import TeamsInfo

member = await TeamsInfo.get_member(context, member_id)
email = getattr(member, "email", None)
```

## 6. `microsoft_agents.hosting.core.authorization`

### What you use

- `ClaimsIdentity`

### What it provides

Represents the authenticated identity and claims used when continuing a conversation proactively.

### Example

```python
from microsoft_agents.hosting.core.authorization import ClaimsIdentity

claims_identity = ClaimsIdentity(
    claims={
        "aud": microsoft_app_id,
        "appid": microsoft_app_id,
    },
    authentication_type="proactive",
    is_authenticated=True,
)
```

## How These Pieces Fit Together

The runtime flow is roughly:

1. Load config from environment.
2. Create an MSAL connection manager.
3. Create a `CloudAdapter`.
4. Create an `AgentApplication`.
5. Register handlers like `@agent_app.activity("message")`.
6. Use `TurnContext` and `MessageFactory` to reply.
7. Use `ConversationReference` and `ClaimsIdentity` for proactive messages later.

## Minimal End-To-End Example From This Repo Pattern

```python
from os import environ
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import AgentApplication, Authorization, MemoryStorage, TurnState

agents_sdk_config = load_configuration_from_env(environ)
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)

storage = MemoryStorage()
authorization = Authorization(storage, connection_manager, **agents_sdk_config)

agent_app = AgentApplication[TurnState](
    storage=storage,
    authorization=authorization,
)
```

## Short Version

- `activity`: Activity models and config loading.
- `authentication.msal`: Bot auth wiring.
- `hosting.aiohttp`: HTTP hosting and adapter pipeline.
- `hosting.core`: Bot app model, turn context, message helpers, storage.
- `hosting.teams`: Teams-specific helpers.
- `hosting.core.authorization`: Claims and identity support for proactive flows.