# Python Teams Bot Sample

This is a lean study sample that shows what a Python Teams bot can look like for the Foundry command model.

It demonstrates:

- Bot Framework-style Teams message handling in Python
- command parsing for:
  - `build it`
  - `build it <model>`
  - `list builds`
  - `build status <resource-group>`
  - `teardown <resource-group>`
  - `heartbeat`
  - `listener status`
  - `help`
- local conversation reference persistence
- queue handoff using JSON job files
- a sample PowerShell worker handoff script

This sample is intentionally small and study-focused:

- it uses a local file-backed queue instead of Azure Storage Queue
- it uses a local JSON file instead of durable table storage
- the PowerShell worker script shows the intended handoff contract and does not execute production deployment commands by default

## Files

- `app.py` - aiohttp host and Bot Framework adapter wiring
- `bot.py` - Teams bot command handling
- `command_parser.py` - exact command model parser
- `conversation_store.py` - local JSON conversation reference store
- `job_dispatcher.py` - file-backed queue writer
- `models.py` - command and job models
- `sample-worker.ps1` - PowerShell handoff example
- `requirements.txt` - Python dependencies
- `teams-bot-automation-implementation-guide.md` - detailed LLM-oriented implementation guide
- `teams-bot-automation-architecture-overview.docx` - human-readable architecture overview

## Run locally

```powershell
cd deployment\python-teams-bot-sample
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MicrosoftAppId = "<bot-app-id>"
$env:MicrosoftAppPassword = "<bot-secret>"
python app.py
```

The sample host listens on `http://localhost:3978/api/messages`.

## Queue handoff

When the bot receives `build it`, `teardown`, `list builds`, or `build status`, it writes a JSON job file into:

```text
deployment\python-teams-bot-sample\.queue\pending
```

You can inspect the file or pass it to the sample PowerShell worker:

```powershell
pwsh .\sample-worker.ps1 -JobPath .\.queue\pending\<job-file>.json
```

## Notes

- The heartbeat message format mirrors the current Teams listener style.
- The build/teardown queued acknowledgements are shaped for the same operational workflow you already use.
- For production, replace the file queue with Azure Storage Queue and replace the local conversation store with Table Storage or Cosmos DB.
