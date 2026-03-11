# ════════════════════════════════════════════════════════════════
# Dockerfile — Bot-the-Builder container image
#
# Bundles: Python 3.14 + PowerShell 7.4 + Azure CLI + Bicep +
#          Azure Developer CLI (azd) + Az/Graph PowerShell modules
#
# Build:
#   docker build -t zolab-bot:latest -f bot-app/Dockerfile .
#
# Run locally:
#   docker run -p 8000:8000 --env-file bot-app/python-teams-bot-sample/.env zolab-bot:latest
# ════════════════════════════════════════════════════════════════

FROM python:3.13-slim-bookworm AS base

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies ──────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        apt-transport-https \
        ca-certificates \
        curl \
        gnupg \
        lsb-release \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# ── PowerShell 7.4 ──────────────────────────────────────────────
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/microsoft-debian-bookworm-prod bookworm main" \
        > /etc/apt/sources.list.d/microsoft.list \
    && apt-get update && apt-get install -y --no-install-recommends powershell \
    && rm -rf /var/lib/apt/lists/*

# ── Azure CLI ────────────────────────────────────────────────────
RUN pip install --no-cache-dir azure-cli

# ── Bicep CLI (extension of Azure CLI) ───────────────────────────
RUN az bicep install

# ── Azure Developer CLI (azd) ───────────────────────────────────
RUN curl -fsSL https://aka.ms/install-azd.sh | bash

# ── PowerShell modules (Az + Microsoft.Graph) ───────────────────
RUN pwsh -NoProfile -Command " \
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; \
    Install-Module -Name Az.Accounts       -Scope AllUsers -Force; \
    Install-Module -Name Az.Resources      -Scope AllUsers -Force; \
    Install-Module -Name Az.Storage        -Scope AllUsers -Force; \
    Install-Module -Name Az.CognitiveServices -Scope AllUsers -Force; \
    Install-Module -Name Microsoft.Graph.Authentication -Scope AllUsers -Force; \
    Install-Module -Name Microsoft.Graph.Groups         -Scope AllUsers -Force; \
    Install-Module -Name Microsoft.Graph.Users          -Scope AllUsers -Force; \
    "

# ── Application layout ──────────────────────────────────────────
#
#   /app/
#     bot-app/python-teams-bot-sample/   ← workdir (bot code)
#     deployment/                        ← PowerShell scripts + Bicep
#
# This matches app.py's path resolution:
#   BASE_PATH.parent.parent / "deployment" / "deploy-foundry-env.ps1"
# ─────────────────────────────────────────────────────────────────

WORKDIR /app

# Copy deployment scripts + Bicep templates
COPY deployment/ /app/deployment/

# Copy bot application code
COPY bot-app/python-teams-bot-sample/ /app/bot-app/python-teams-bot-sample/

# ── Python dependencies ──────────────────────────────────────────
RUN pip install --no-cache-dir -r /app/bot-app/python-teams-bot-sample/requirements.txt

# ── Startup ──────────────────────────────────────────────────────
WORKDIR /app/bot-app/python-teams-bot-sample
EXPOSE 8000
ENV PORT=8000

CMD ["python", "-m", "aiohttp.web", "-H", "0.0.0.0", "-P", "8000", "app:create_app"]
