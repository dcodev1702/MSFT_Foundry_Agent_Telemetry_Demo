# ════════════════════════════════════════════════════════════════
# Dockerfile — Bot-the-Builder container image
#
# Overlays the latest bot code onto a known-good bot runtime image
# stored in the bot ACR. This avoids transient Docker Hub pull-rate
# failures during ACR tasks while preserving the existing runtime toolchain.
#
# Build:
#   docker build -t zolab-bot:latest -f bot-app/Dockerfile .
#
# Run locally:
#   docker run -p 8000:8000 --env-file bot-app/python-teams-bot-sample/.env zolab-bot:latest
# ════════════════════════════════════════════════════════════════

ARG BASE_IMAGE=zolabbotacrbotprd.azurecr.io/zolab-bot:botfix-20260311173803-756bbc6
FROM ${BASE_IMAGE} AS base

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

# Copy the Teams package icon so the bot can serve it publicly for Teams avatars
COPY bot-app/teams-app/color.png /app/bot-app/python-teams-bot-sample/assets/bot-icon.png

# ── Python dependencies ──────────────────────────────────────────
RUN pip install --no-cache-dir -r /app/bot-app/python-teams-bot-sample/requirements.txt

# ── Startup ──────────────────────────────────────────────────────
WORKDIR /app/bot-app/python-teams-bot-sample
EXPOSE 8000
ENV PORT=8000

CMD ["python", "-m", "aiohttp.web", "-H", "0.0.0.0", "-P", "8000", "app:create_app"]
