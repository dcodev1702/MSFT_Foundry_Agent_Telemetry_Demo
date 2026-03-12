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
#   docker run -p 8000:8000 --env-file bot-app/runtime/.env zolab-bot:latest
# ════════════════════════════════════════════════════════════════

ARG BASE_IMAGE=zolabbotacrbotprd.azurecr.io/zolab-bot:botfix-20260311173803-756bbc6
FROM ${BASE_IMAGE} AS base

# ── Application layout ──────────────────────────────────────────
#
#   /app/
#     bot-app/runtime/                   ← runtime root
#       src/                            ← workdir (bot code)
#     deployment/                        ← PowerShell scripts + Bicep
#
# This matches app.py's path resolution:
#   BASE_PATH.parent.parent.parent / "deployment" / "deploy-foundry-env.ps1"
# ─────────────────────────────────────────────────────────────────

WORKDIR /app

# Copy deployment scripts + Bicep templates
COPY deployment/ /app/deployment/

# Copy bot application code
COPY bot-app/runtime/ /app/bot-app/runtime/

# Copy the Teams package icon so the bot can serve it publicly for Teams avatars
COPY bot-app/teams-app/color.png /app/bot-app/runtime/src/assets/bot-icon.png

# Remove any inherited local env file so production only uses platform-provided settings.
RUN rm -f /app/bot-app/runtime/.env

# ── Python dependencies ──────────────────────────────────────────
RUN pip install --no-cache-dir -r /app/bot-app/runtime/requirements.txt

# ── Startup ──────────────────────────────────────────────────────
WORKDIR /app/bot-app/runtime/src
EXPOSE 8000
ENV PORT=8000

CMD ["python", "-m", "aiohttp.web", "-H", "0.0.0.0", "-P", "8000", "app:create_app"]
