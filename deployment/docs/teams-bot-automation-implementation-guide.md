# Teams Bot + Automation App Implementation Guide

## Purpose

This document is the build specification for replacing the current user-delegated Teams listener with an app-based solution that:

- receives commands from Microsoft Teams through a bot
- runs Azure deployment and teardown actions through a non-interactive automation identity
- sends status, confirmations, and final results back to Teams
- preserves the current deployment capabilities without depending on a user's live Graph session

This guide is written for an LLM or automation-oriented builder. It is intentionally explicit, task-driven, and implementation-focused.

---

## 1. Target Outcome

Build a single-tenant solution for the `dibsecurity.onmicrosoft.com` tenant with these properties:

1. A Teams app with a bot is installed into a dedicated operational Teams channel, not a 1:1 chat.
2. The bot accepts commands such as:
   - `build it`
   - `heartbeat`
   - `list builds`
   - `build status <resource-group>`
   - `teardown <resource-group>`
   - `listener status`
   - `help`
3. The bot validates the caller against the approved tenant, approved team/channel scope, and any required allowlist.
4. The bot writes a job record and immediately acknowledges receipt.
5. A background worker executes the requested operation by calling hardened PowerShell entry points.
6. The worker authenticates with a certificate-based automation app registration, not a human user.
7. Status updates and final responses are sent back to the originating Teams channel thread using bot proactive messaging.
8. The build still creates the same Azure Foundry environment and preserves the existing deployment behaviors already implemented in `deployment\deploy-foundry-env.ps1`.

Non-goals:

- Do not use delegated Graph chat permissions for the production messaging path.
- Do not rely on a Foundry Agent for privileged execution.
- Do not introduce unnecessary microservices or a large orchestration platform for a small operational workflow.

---

## 2. Recommended Architecture

## 2.1 Components

1. **Teams Bot App**
   - Single-tenant Teams application package.
   - Provides the bot entry point for chat/channel commands.
   - Supports proactive replies back into the same conversation.

2. **Bot API Host**
   - Azure App Service.
   - Exposes `/api/messages`.
   - Validates inbound Teams channel activities and parses commands.
   - Stores conversation references and enqueues jobs.

3. **Job Queue**
   - Azure Storage Queue.
   - Decouples Teams request/response from long-running deployments.
   - Prevents Teams request timeouts.

4. **Worker**
   - Azure Function or WebJob-style background worker.
   - Dequeues jobs.
   - Executes PowerShell commands non-interactively.
   - Publishes progress and final result messages.

5. **Automation App Registration**
   - Microsoft Entra application used by the worker.
   - Authenticates with certificate credentials.
   - Holds Azure RBAC and Graph application permissions.

6. **State Store**
   - Azure Table Storage or Cosmos DB Table API is sufficient.
   - Stores:
     - job records
     - conversation references
     - build metadata
     - command audit trail

7. **Existing Deployment Assets**
   - `deployment\deploy-foundry-env.ps1`
   - `deployment\main.bicep`
   - `deployment\law-rbac.bicep`
   - `deployment\modules\*`

## 2.2 Logical Flow

1. User sends `build it` in Teams.
2. Bot receives the activity and verifies the caller.
3. Bot writes a job record with status `pending-confirmation` or `queued`.
4. Bot sends a confirmation card or a short confirmation prompt.
5. On confirmation, the bot enqueues a job.
6. Worker dequeues the job and authenticates with:
   - Azure using the automation app registration
   - Microsoft Graph using the same automation app registration where directory operations are required
7. Worker invokes a non-interactive PowerShell entry point.
8. Worker emits progress events at the same cadence currently used by the script.
9. Bot uses stored conversation references to proactively post updates and the final status back into the same Teams channel thread.
10. Worker writes durable result metadata for later `build status` and `list builds` requests.

## 2.3 Why This Design

- Teams bot messaging is the supported path for interactive app messaging in Teams.
- Graph application permissions alone are not a good fit for normal interactive Teams send/reply behavior.
- Queue + worker is the minimum safe split for long-running jobs.
- Reusing the existing PowerShell/Bicep assets keeps the scope controlled.

---

## 3. Technology Choices

Use the following unless there is a strong implementation constraint:

| Area | Recommendation | Why |
|---|---|---|
| Bot host | Python with Bot Framework SDK and a lightweight web host such as FastAPI | Aligns better with the existing Python-heavy notebook workflow and keeps the bot/worker stack closer to the current repo skillset |
| Worker | PowerShell 7 running from Azure Functions or App Service WebJob | Reuses existing deployment scripts directly |
| Queue | Azure Storage Queue | Simple, cheap, sufficient |
| State | Azure Table Storage | Lightweight, easy for job and conversation metadata |
| Auth | Entra app registration with certificate | Non-interactive and durable |
| Hosting | Azure App Service for bot, Function App or WebJob for worker | Operationally simple |
| IaC | Extend existing Bicep | Consistent with current repository patterns |
| Observability | Application Insights + structured logs | Already aligned to current environment |

TypeScript is still a valid option and has strong Teams samples, but it is not compelling enough here to override the benefits of Python alignment with the existing repo, notebooks, and PowerShell-oriented automation flow.

---

## 4. Identity and Permission Model

## 4.1 Separate Identities

Use two identities:

1. **Teams bot identity**
   - Used for bot authentication and Teams channel integration.
   - Only handles message ingress/egress.

2. **Automation app identity**
   - Used by the worker to perform Azure and Graph operations.
   - Does not receive Teams traffic directly.

This split reduces blast radius and keeps privilege boundaries clear.

## 4.2 Azure RBAC for the Automation App

Grant the automation app only the scopes required to perform the existing deployment behavior.

### Required Azure RBAC

| Scope | Role | Purpose |
|---|---|---|
| `zolab` subscription or constrained deployment scope | Contributor | Create and manage resource groups and deployed resources |
| `zolab` subscription or constrained deployment scope | Role Based Access Control Administrator | Create RG-level role assignments during deployment |
| Security subscription scope that contains the DIBSecCom LAW assignment target | Role Based Access Control Administrator | Assign LAW Reader role to the target principal at the correct security scope |
| Security subscription scope that contains monitoring resources referenced by deployment | Reader or Contributor as needed by the script | Read workspace and related metadata |

Notes:

- If the script only needs to assign an existing LAW role, use the narrowest scope that covers that LAW resource.
- Prefer **Role Based Access Control Administrator** over broader standing access.
- If an environment-specific resource group can contain the deployment surface, scope Contributor and RBAC admin there instead of at full subscription level.

## 4.3 Microsoft Graph Application Permissions for the Automation App

### Baseline application permissions

| Permission | Required | Purpose |
|---|---|---|
| `User.Read.All` | Yes | Resolve users by UPN/object ID |
| `Group.Read.All` or `Group.ReadWrite.All` | Yes | Read existing security group metadata |
| `GroupMember.ReadWrite.All` | Yes | Add/remove members in `zolab-ai-dev` |

### Optional application permissions

| Permission | When needed | Purpose |
|---|---|---|
| `Group.ReadWrite.All` | If the automation must create/update the group | Create and manage the `zolab-ai-dev` group |
| `Directory.Read.All` | Only if broader directory reads are genuinely required | Broader directory discovery |
| `RoleManagement.ReadWrite.Directory` | Only if the group is made role-assignable in Entra | Manage role-assignable directory roles |

Guidance:

- Do not grant more than needed.
- If the group can be created once manually, omit `Group.ReadWrite.All` and keep the app limited to reading the group plus managing membership.
- The app does **not** need standing Global Administrator.

## 4.4 Teams Permissions

Do not design the solution around Graph application message send permissions for normal chat operations.

Use:

- Teams app manifest
- Bot Framework / Teams bot channel
- proactive bot messaging

This is the supported operational pattern for interactive command/reply workflows.

---

## 5. Repository Changes to Make

Create a new bot/worker surface without breaking the current PowerShell-first deployment path.

## 5.1 New Directories

Add a structure similar to:

```text
deployment/
  bot/
    app.py
    bot.py
    requirements.txt
    commands/
      build.py
      teardown.py
      build_status.py
      list_builds.py
      heartbeat.py
      listener_status.py
      help.py
    services/
      queue_service.py
      conversation_store.py
      authorization_service.py
      status_formatter.py
    cards/
      confirmation_card.py
    models/
      commands.py
      jobs.py
  worker/
    run-job.ps1
    modules/
      invoke-build.ps1
      invoke-teardown.ps1
      invoke-build-status.ps1
      invoke-list-builds.ps1
      invoke-heartbeat.ps1
      common.ps1
  infra/
    bot-resources.bicep
    worker-resources.bicep
    app-registrations-notes.md
  teams-app/
    manifest.json
    color.png
    outline.png
```

The exact layout can vary, but preserve a clear split between bot ingress and privileged worker execution.

## 5.2 Existing File Refactors

Refactor, do not rewrite from scratch, these files:

| Existing file | Required action |
|---|---|
| `deployment\deploy-foundry-env.ps1` | Expose non-interactive entry points and machine-readable output |
| `deployment\teams-command-dispatch.ps1` | Keep temporarily for rollback; mark as legacy path |
| `deployment\teams-chat.ps1` | Keep only if some formatting helpers are reusable; do not use it as the primary Teams transport in the target design |
| `deployment\README.md` | Add references to the new app-based design documents and future operational path |

---

## 6. PowerShell Refactor Requirements

The worker must be able to execute the same operations without prompts.

## 6.1 Required entry points

Create non-interactive worker-facing commands for:

1. `Invoke-FoundryBuild`
2. `Invoke-FoundryTeardown`
3. `Get-FoundryBuildStatus`
4. `Get-FoundryBuildInventory`
5. `Get-FoundryListenerHeartbeat` or equivalent environment health check

## 6.2 Input contract

Each command should accept explicit parameters rather than relying on human prompts.

Examples:

- requested by user UPN or Entra object ID
- conversation/job ID
- model choice
- target resource group
- notifier callback endpoint or job status file path

## 6.3 Output contract

Each command must produce structured JSON to stdout or a well-known output file. Include:

- job ID
- operation
- status
- resource group
- timestamps
- requested by
- summary text
- error details when failed
- important resource outputs when successful

## 6.4 Progress reporting

The worker must emit progress checkpoints that the bot can convert into Teams messages. Preserve current expectations:

- build progress updates roughly every 1 minute while active
- teardown progress updates roughly every 1 minute while active
- build progress message text: `🚧 One moment ..the Bob's are still building! 🚧`
- teardown progress message text: `🚧 Pls hold while we teardown: <resource-group> 🚧`
- final status with details

## 6.5 Backward compatibility

Do not remove the current manual CLI flow until the bot path is proven in test and pilot use.

---

## 7. Teams Bot Build Tasks

## 7.1 Create the bot application

Tasks:

1. Create a new Python bot project in `deployment\bot`.
2. Configure the Python Bot Framework adapter and Teams channel activity handling.
3. Accept only the supported commands.
4. Normalize commands to a strict internal command model.
5. Return a help message for unknown commands.

Acceptance criteria:

- Bot starts locally.
- `/api/messages` responds to Teams traffic.
- Bot can parse all supported commands.

## 7.2 Store conversation references

Tasks:

1. On each message or install event, capture:
   - conversation ID
   - service URL
   - tenant ID
   - bot ID
   - channel data needed for proactive responses
2. Save the reference durably.
3. Upsert by conversation scope.

Acceptance criteria:

- A later worker process can send a proactive message back to the same conversation without requiring a new inbound user message.

## 7.3 Implement confirmation flow

Tasks:

1. For destructive or expensive commands, send an Adaptive Card or simple structured confirmation prompt.
2. Track pending confirmations by:
   - requestor
   - command
   - target
   - expiration time
3. Reject confirmations from the wrong user.

Acceptance criteria:

- `build it`, `build status`, and `teardown` can require confirmation.
- Expired confirmations are rejected cleanly.

## 7.4 Implement authorization

Tasks:

1. Add a simple authorization layer.
2. Restrict to:
   - a specific tenant
   - optionally a specific team/channel or specific allowlisted users/group
3. Log denials.

Acceptance criteria:

- Unauthorized users do not trigger jobs.

## 7.5 Implement proactive updates

Tasks:

1. Add a service that uses the stored conversation reference to continue the conversation proactively.
2. Support:
   - acknowledgement
   - progress update
   - success
   - failure
   - heartbeat/listener status response

Acceptance criteria:

- Worker can publish back to Teams after the initial request is complete.

---

## 8. Worker Build Tasks

## 8.1 Queue processor

Tasks:

1. Create a queue-triggered worker.
2. Define a job schema:
   - job ID
   - command type
   - arguments
   - requester identity
   - conversation reference key
   - correlation ID
3. Mark state transitions:
   - queued
   - running
   - succeeded
   - failed
   - cancelled

Acceptance criteria:

- Jobs are processed exactly once or retried safely.

## 8.2 Non-interactive authentication

Tasks:

1. Load certificate or managed secret reference.
2. Authenticate to Azure using the automation app.
3. Authenticate to Graph using the automation app when directory operations are required.
4. Fail fast with explicit diagnostics if either auth path is broken.

Acceptance criteria:

- Worker runs without interactive login.

## 8.3 PowerShell execution wrapper

Tasks:

1. Create wrapper scripts that call refactored deployment functions.
2. Capture:
   - stdout
   - stderr
   - exit code
   - structured JSON result
3. Translate script output into job state and Teams status messages.

Acceptance criteria:

- Build/teardown/status/list commands can be executed by the worker predictably.

## 8.4 Progress publishing

Tasks:

1. Publish startup message.
2. Publish periodic in-progress messages.
3. Publish final formatted result.
4. Include correlation/job ID in logs.

Acceptance criteria:

- Teams receives meaningful progress from the worker for long-running jobs.

---

## 9. Data Model Requirements

Use simple tables or equivalent documents for:

## 9.1 Conversation references

Fields:

- `PartitionKey`
- `RowKey`
- `tenantId`
- `conversationId`
- `serviceUrl`
- `botAppId`
- `scopeType`
- `teamId` when applicable
- `channelId` when applicable
- `lastSeenUtc`

## 9.2 Jobs

Fields:

- `jobId`
- `operation`
- `status`
- `requestedByUpn`
- `requestedByObjectId`
- `resourceGroup`
- `model`
- `conversationRefKey`
- `submittedUtc`
- `startedUtc`
- `completedUtc`
- `summary`
- `detailsJson`
- `buildInfoPath`
- `correlationId`

## 9.3 Build registry

Fields:

- `resourceGroup`
- `requestedBy`
- `createdUtc`
- `status`
- `foundryProjectEndpoint`
- `azureOpenAiEndpoint`
- `model`
- `buildInfoFile`

---

## 10. Detailed Implementation Phases

## Phase 0 - Design freeze and scoping

Tasks:

1. Confirm the solution is single-tenant.
2. Confirm the primary Teams operating surface is one dedicated channel in the approved team; do not use a 1:1/chat install.
3. Confirm whether the automation app must create the Entra group or only manage membership.
4. Confirm whether `build it` still needs interactive model selection or should accept a command argument.

Preferred default:

- use a dedicated Teams channel for visibility and shared operational history
- accept `build it <model>` and also support a confirmation card with model buttons
- keep group creation as bootstrap-only, not routine worker behavior

Deliverable:

- final design assumptions written into repository docs

## Phase 1 - App registrations and credentials

Tasks:

1. Create the bot app registration.
2. Create the automation app registration.
3. Generate and store a certificate for the automation app.
4. Upload public cert to the app registration.
5. Record application IDs, tenant ID, and secret/cert references in secure configuration.

Deliverable:

- both app identities exist and can authenticate

## Phase 2 - Azure and Graph permissions

Tasks:

1. Assign Azure RBAC to the automation app.
2. Add Graph application permissions.
3. Grant admin consent.
4. Validate:
   - user lookup
   - group membership management
   - Azure role assignment creation
   - deployment resource creation

Deliverable:

- automation identity can perform required infrastructure and directory actions

## Phase 3 - Bot skeleton

Tasks:

1. Create the bot host.
2. Register `/api/messages`.
3. Add command parsing.
4. Add help/usage output.
5. Add conversation reference persistence.

Deliverable:

- bot is reachable from Teams and stores conversation references

## Phase 4 - Worker skeleton

Tasks:

1. Create the queue processor.
2. Define job contracts.
3. Add authentication bootstrap.
4. Add PowerShell invocation wrapper.

Deliverable:

- queued jobs can run a mock command and report completion

## Phase 5 - Refactor deployment script

Tasks:

1. Remove interactive assumptions from worker-facing execution paths.
2. Add structured output.
3. Add explicit error codes.
4. Preserve existing manual CLI behavior.

Deliverable:

- worker can call the deployment logic non-interactively

## Phase 6 - End-to-end command support

Tasks:

1. Implement `heartbeat`.
2. Implement `listener status`.
3. Implement `list builds`.
4. Implement `build status`.
5. Implement `build it`.
6. Implement `teardown`.

Deliverable:

- all current listener commands work through the bot path

## Phase 7 - Hardening

Tasks:

1. Add caller authorization.
2. Add idempotency checks.
3. Add retry-safe queue handling.
4. Add logging and correlation IDs.
5. Add operational health endpoint.

Deliverable:

- safe pilot-ready solution

## Phase 8 - Pilot and cutover

Tasks:

1. Run side-by-side with the legacy listener.
2. Validate every command.
3. Document operator workflow.
4. Cut over Teams users to the bot.
5. Keep the legacy listener only as rollback.

Deliverable:

- bot becomes the primary path

---

## 11. Command-by-Command Behavior

## 11.1 `build it`

Required behavior:

1. Bot validates requester.
2. Bot asks for model if not provided.
3. Bot requests confirmation.
4. Worker runs deployment.
5. Worker posts progress every 1 minute using `🚧 One moment ..the Bob's are still building! 🚧`.
6. Worker posts final success/failure with key outputs.
7. Worker stores build metadata for later lookup.

Exact build-complete message contract:

- Preserve the exact success banner text: `● ☁️🎉🚀 Fresh build — all green!`
- Preserve the exact closing text: `Ready for the notebook! 🎯`
- Preserve the exact item labels shown below
- Preserve the box-drawing layout; column widths may expand dynamically based on content length

Example build-complete message:

```text
● ☁️🎉🚀 Fresh build — all green!

┌───────────────────────────────┬──────────────────────────────────────────────────────────────────────────────┐
│ Item                          │ Status                                                                       │
├───────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
│ ☁️ Resource Group             │ ✅ zolab-ai-<suffix>                                                         │
│ 🗄️ Storage                    │ <storage-account-name>                                                       │
│ 🔐 Key Vault                  │ <key-vault-name>                                                             │
│ 📊 App Insights               │ <app-insights-name>                                                          │
│ 🤖 AI Foundry                 │ <ai-foundry-name>                                                            │
│ 🏢 AI Project                 │ <ai-project-name>                                                            │
│ 🧠 Model                      │ <deployment-name> (<sku-name>)                                               │
│ 📝 Build Info                 │ build_info-<suffix>.json ✅                                                  │
│ 🔗 App Insights Connection    │ <connection-status>                                                          │
│ 👁️ App Insights Access       │ <app-insights-access-status>                                                 │
│ 📡 LAW RBAC                   │ Log Analytics Reader on DIBSecCom ✅                                         │
│ 👤 User                       │ <user-upn> added to zolab-ai-dev ✅                                          │
│ 🔌 Foundry Project Endpoint   │ <foundry-project-endpoint>                                                   │
│ 🤖 Azure OpenAI Endpoint      │ <azure-openai-endpoint>                                                      │
└───────────────────────────────┴──────────────────────────────────────────────────────────────────────────────┘

Ready for the notebook! 🎯
```

## 11.2 `teardown <resource-group>`

Required behavior:

1. Bot validates target format.
2. Bot asks for confirmation.
3. Worker runs targeted cleanup.
4. Worker preserves shared access behaviors already built into the PowerShell logic.
5. Worker posts `🚧 Pls hold while we teardown: <resource-group> 🚧` every 1 minute while teardown is active.
6. Worker posts completion status.

## 11.3 `build status <resource-group>`

Required behavior:

1. Bot validates target.
2. Optionally confirms.
3. Worker returns machine-readable build state and important outputs.

## 11.4 `list builds`

Required behavior:

1. Worker reads the durable build registry and/or `build_info-*.json`.
2. Response includes:
   - resource group
   - requested by
   - created date
   - model
   - status

## 11.5 `heartbeat`

Required behavior:

1. Return bot health, worker queue health, and identity context summary.
2. Do not rely on human Graph context.

Exact heartbeat message contract:

- Preserve the exact labels and emoji prefixes shown below
- Keep the message as a per-line health readout
- Replace only the dynamic values

Example heartbeat message:

```text
🟢 Status: Online ✅
📜 Script: teams-command-dispatch.ps1
🆔 PID: <pid>
🖥️ pwsh version: <pwsh-version>
⏱️ Uptime: <uptime>
🧠 Memory: <memory-usage>
💬 Last response: <last-response-utc-or-default-text>
🔗 Graph API: <Connected|Disconnected> 🔌
📢 Listening in: <teams-channel-or-topic>
👤 Identity: <account-upn>
🕒 Checked at: <utc-timestamp>
```

## 11.6 `listener status`

Rename internally if desired, but preserve the user-facing command for continuity.

Required behavior:

1. Return:
   - bot status
   - worker status
   - queue depth
   - app identity names
   - current UTC time

---

## 12. Security Requirements

1. Use certificate auth for the automation app.
2. Store secrets/cert references in Key Vault.
3. Do not store tokens in files.
4. Restrict Teams usage to approved users/channels.
5. Log administrative actions and destructive commands.
6. Add correlation IDs to every job.
7. Use least privilege for Azure RBAC and Graph app permissions.
8. Prefer bootstrap-time creation of Entra groups over recurring runtime directory mutation where possible.

---

## 13. Observability Requirements

1. Send bot and worker logs to Application Insights.
2. Log these events:
   - command received
   - authorization pass/fail
   - confirmation sent
   - job queued
   - job started
   - progress update sent
   - job succeeded/failed
3. Track:
   - job duration
   - queue latency
   - failure count
   - command counts by type
4. Add a simple health endpoint for the bot host.

---

## 14. Testing Requirements

## 14.1 Unit tests

Cover:

- command parsing
- authorization decisions
- confirmation expiration
- job payload validation
- status formatting

## 14.2 Integration tests

Cover:

- bot receives Teams message
- conversation reference is stored
- job is enqueued
- worker dequeues and invokes mock PowerShell
- proactive completion message is sent

## 14.3 End-to-end tests

Cover:

- build happy path
- teardown happy path
- build status lookup
- list builds
- unauthorized user
- expired confirmation
- worker auth failure
- Azure RBAC failure
- Graph application permission failure

---

## 15. Migration Strategy

1. Keep the current `teams-command-dispatch.ps1` listener operational until the bot path is validated.
2. Implement the bot path in parallel.
3. Pilot with a limited set of users or a dedicated Teams channel.
4. Compare outputs for:
   - build success
   - teardown success
   - progress cadence
   - status formatting
5. After stable pilot acceptance:
   - disable legacy listener startup
   - retain legacy scripts for rollback

Rollback plan:

- if the bot path fails, users resume sending commands through the current PowerShell listener while the bot is repaired

---

## 16. Definition of Done

The project is complete when all of the following are true:

1. Teams bot is installed and reachable.
2. Bot can receive and validate commands.
3. Bot can store and use conversation references for proactive replies.
4. Worker authenticates non-interactively with the automation app.
5. Worker can deploy, list, query, and teardown environments.
6. Worker posts progress and final status to Teams.
7. No human Graph token is required for normal operation.
8. Documentation exists for both operators and builders.
9. Legacy listener remains available only as rollback, not as the primary architecture.

---

## 17. Execution Checklist

Use this checklist in order:

1. Create bot app registration.
2. Create automation app registration.
3. Assign Azure RBAC to automation app.
4. Add Graph application permissions and admin consent.
5. Build bot skeleton and `/api/messages`.
6. Add conversation reference persistence.
7. Add queue and job schema.
8. Build worker skeleton.
9. Refactor PowerShell into non-interactive functions.
10. Implement `heartbeat` and `listener status`.
11. Implement `list builds` and `build status`.
12. Implement `build it`.
13. Implement `teardown`.
14. Add Application Insights telemetry and correlation IDs.
15. Test end-to-end in a pilot Teams surface.
16. Update operational documentation and cut over.

---

## 18. Detailed App Registration Instructions

## 18.1 Create the Teams bot app registration

Perform these steps:

1. Create a single-tenant Microsoft Entra application for the bot.
2. Record:
   - application (client) ID
   - directory (tenant) ID
3. Configure the bot host redirect and messaging endpoint settings required by the chosen bot framework stack.
4. Associate the app with the Teams bot/channel registration.
5. Ensure the bot is configured for the Teams channel.

Required output:

- bot app ID
- tenant ID
- messaging endpoint URL

## 18.2 Create the automation app registration

Perform these steps:

1. Create a separate single-tenant Entra application for automation.
2. Generate a certificate specifically for this app.
3. Upload the public certificate to the app registration.
4. Store the certificate securely so the worker can access it at runtime.
5. Record:
   - client ID
   - tenant ID
   - certificate thumbprint or Key Vault secret/certificate reference

Required output:

- automation app client ID
- tenant ID
- certificate reference

## 18.3 Apply Graph application permissions

Perform these steps:

1. Add the baseline Graph application permissions:
   - `User.Read.All`
   - `Group.Read.All` or `Group.ReadWrite.All`
   - `GroupMember.ReadWrite.All`
2. Add optional permissions only if required by the final design:
   - `Group.ReadWrite.All`
   - `Directory.Read.All`
   - `RoleManagement.ReadWrite.Directory`
3. Grant admin consent.
4. Validate with a non-interactive token test before moving on.

Validation tasks:

1. Resolve the target deployment user by UPN.
2. Read the `zolab-ai-dev` group.
3. Add and remove a test user in a non-production group if available.

## 18.4 Apply Azure RBAC

Perform these steps:

1. Assign `Contributor` at the intended Zolab deployment scope.
2. Assign `Role Based Access Control Administrator` at the intended Zolab deployment scope.
3. Assign `Role Based Access Control Administrator` at the narrowest Security scope that covers the LAW role assignment target.
4. Add a read-capable role on the monitoring scope if the deployment logic must inspect monitoring resources.
5. Validate by creating a harmless test role assignment in a sandbox scope if available.

---

## 19. Detailed Infrastructure Build Instructions

## 19.1 Bot host infrastructure

Provision:

- one App Service plan
- one App Service for the bot API
- one Application Insights resource
- configuration settings for bot credentials and storage references

Configure the bot App Service with:

- HTTPS only
- managed identity if needed for Key Vault access
- Key Vault references for sensitive settings
- deployment slot only if you want blue/green rollout

## 19.2 Worker infrastructure

Provision either:

- an Azure Function App with queue trigger, or
- an App Service WebJob attached to the bot host or a separate host

Preferred default:

- Function App if you want clean queue-trigger execution
- WebJob if you want to stay extremely close to PowerShell process execution

The worker host must have:

- access to the automation certificate or Key Vault reference
- Storage Queue connection
- state store connection
- Application Insights telemetry

## 19.3 Storage resources

Provision one storage account with:

- one queue for commands, for example `teams-command-jobs`
- one poison queue for failed jobs
- one table for conversation references
- one table for job records
- one table for build inventory if you want inventory independent of local JSON files

## 19.4 Key Vault

Use Key Vault to store:

- automation certificate reference
- any app secrets if temporarily required
- operational configuration that should not live in plain text

---

## 20. Detailed Bot Implementation Instructions

## 20.1 Required bot behaviors

Implement these bot handlers:

1. message activity handler
2. conversation update handler
3. adaptive card submit handler if confirmation cards are used
4. proactive messaging service

## 20.2 Command parsing rules

Normalize inbound text by:

1. trimming whitespace
2. converting to a normalized lowercase command token
3. preserving case-sensitive parameters only where needed
4. extracting arguments with strict patterns

Suggested normalized command forms:

- `build`
- `build-status`
- `list-builds`
- `teardown`
- `heartbeat`
- `listener-status`
- `help`

Reject ambiguous input with a help response instead of guessing.

## 20.3 Authorization rules

At minimum enforce:

1. tenant match
2. approved chat/team/channel scope
3. approved user or approved Entra group membership if desired

Suggested behavior:

- deny with a friendly message in Teams
- log the denial with correlation ID and caller information

## 20.4 Confirmation design

For `build it`, `build status`, and `teardown`:

1. create a pending confirmation record
2. include request summary
3. include expiration timestamp
4. accept only the original requester as confirmer
5. delete or expire the record after resolution

If using cards, include these actions:

- Confirm
- Cancel

If using text prompts, accept strict responses only.

## 20.5 Immediate acknowledgement pattern

For long-running commands, always send a quick acknowledgement:

- command accepted
- job queued
- job ID
- who requested it
- what will happen next

This prevents users from thinking the bot ignored the request.

---

## 21. Detailed Worker Implementation Instructions

## 21.1 Queue contract

Use a payload shaped like:

```json
{
  "jobId": "guid-or-stable-id",
  "operation": "build|teardown|build-status|list-builds|heartbeat|listener-status",
  "requestedByUpn": "user@dibsecurity.onmicrosoft.com",
  "requestedByObjectId": "entra-object-id",
  "resourceGroup": "zolab-ai-xxxxxx",
  "model": "gpt-4.1-mini",
  "conversationRefKey": "tenant|scope|conversation",
  "submittedUtc": "2026-03-07T22:00:00Z",
  "correlationId": "guid",
  "arguments": {}
}
```

## 21.2 Worker startup sequence

The worker should:

1. dequeue the job
2. load job state
3. authenticate to Azure
4. authenticate to Graph if needed
5. post a `running` update
6. invoke the correct PowerShell entry point
7. persist result state
8. post final success or failure

## 21.3 PowerShell invocation rules

The worker must:

1. call PowerShell 7
2. pass explicit named parameters
3. capture stdout, stderr, and exit code
4. parse JSON result payloads
5. never depend on scraping user-facing decorative output alone

## 21.4 Error handling rules

On failure:

1. capture the exact failing phase
2. preserve the important stderr content
3. update the job status as `failed`
4. send a concise Teams failure summary
5. include a correlation ID or job ID

For retries:

- retry only safe failures such as transient queue or network issues
- do not blindly retry destructive operations without idempotency controls

---

## 22. Required Configuration Surface

The implementation should centralize configuration in environment variables or app settings.

Suggested bot settings:

```text
BOT_APP_ID
BOT_APP_PASSWORD_OR_IDENTITY_REFERENCE
BOT_TENANT_ID
STORAGE_CONNECTION_STRING
STATE_TABLE_CONVERSATIONS
STATE_TABLE_JOBS
STATE_TABLE_BUILDS
COMMAND_QUEUE_NAME
APPINSIGHTS_CONNECTION_STRING
ALLOWED_TENANT_ID
ALLOWED_TEAM_ID
ALLOWED_CHANNEL_ID
ALLOWED_USER_UPNS
```

Suggested worker settings:

```text
AUTOMATION_TENANT_ID
AUTOMATION_CLIENT_ID
AUTOMATION_CERT_THUMBPRINT
AUTOMATION_CERT_KEYVAULT_URI
AZURE_SUBSCRIPTION_NAME_ZOLAB
AZURE_SUBSCRIPTION_NAME_SECURITY
COMMAND_QUEUE_NAME
STATE_TABLE_JOBS
STATE_TABLE_BUILDS
APPINSIGHTS_CONNECTION_STRING
POWERSHELL_SCRIPT_ROOT
```

## 22.1 Configuration hygiene rules

1. Never hardcode tenant IDs or app IDs in source where configuration is more appropriate.
2. Keep channel/team allowlists configurable.
3. Keep environment names and subscription names configurable.
4. Store secret material in Key Vault or managed references.

---

## 23. Documentation and Rollout Tasks

The final implementation should also produce or update:

1. operator runbook
2. rollback runbook
3. app registration inventory
4. RBAC assignment record
5. Teams app installation instructions
6. troubleshooting guide for:
   - bot not responding
   - queue stuck
   - worker auth failure
   - Graph permission failure
   - Azure RBAC failure
   - proactive message failure

During rollout:

1. pilot in a controlled Teams surface
2. compare bot outputs with the legacy listener
3. cut over only after all commands match expected behavior
4. keep the old listener disabled by default but available for rollback until confidence is high

---

## 24. Notes for the Implementing LLM

1. Reuse existing deployment logic rather than re-creating Azure deployment behavior.
2. Keep all destructive operations behind explicit confirmation.
3. Prefer explicit parameter contracts and structured JSON results over log scraping.
4. Preserve the existing shared-access teardown protections already implemented in the PowerShell script.
5. Build the smallest production-safe version first; avoid adding unnecessary orchestration layers.
