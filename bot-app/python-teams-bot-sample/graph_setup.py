# ════════════════════════════════════════════════════════════════
# graph_setup.py — Microsoft Graph helpers for Team + channel setup
#
# Uses the Microsoft Graph SDK (msgraph-sdk) with
# DefaultAzureCredential (Managed Identity on App Service,
# Azure CLI locally).
#
# Required Graph application permissions (admin-consented):
#   Group.ReadWrite.All, Team.Create, Channel.Create
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from azure.identity.aio import DefaultAzureCredential
from msgraph import GraphServiceClient
from msgraph.generated.models.team import Team
from msgraph.generated.models.channel import Channel
from msgraph.generated.models.group import Group
from msgraph.generated.models.teams_app_installation import TeamsAppInstallation

logger = logging.getLogger(__name__)

TEAM_NAME = "Microsoft Foundry Deployments"
CHANNEL_NAME = "bot-the-builder"
CHANNEL_DESCRIPTION = "Foundry Bot operational channel for deployment commands"


class GraphTeamsSetup:
    """One-time setup: create (or find) the operational Team and channel."""

    def __init__(self, credential: DefaultAzureCredential | None = None):
        self._credential = credential or DefaultAzureCredential()
        self._client = GraphServiceClient(
            credentials=self._credential,
            scopes=["https://graph.microsoft.com/.default"],
        )

    async def ensure_team(self, team_name: str = TEAM_NAME) -> str:
        """Create the Team if it doesn't exist. Returns the team (group) ID.

        Uses application permissions — searches all groups, then creates
        the Team if missing.
        """
        # Search for an existing Microsoft 365 group with the right name
        existing = await self._client.groups.get(
            request_configuration=lambda cfg: setattr(
                cfg.query_parameters,
                "filter",
                f"displayName eq '{team_name}' and resourceProvisioningOptions/Any(x:x eq 'Team')",
            )
        )

        if existing and existing.value:
            team_id = existing.value[0].id
            logger.info("Team '%s' already exists — ID: %s", team_name, team_id)
            return team_id

        # Create a new Team (this also creates the backing M365 Group)
        new_team = Team(
            display_name=team_name,
            description="Azure AI Foundry deployment operations managed by the Foundry Bot",
        )
        # Teams creation via POST /teams (requires Team.Create permission)
        result = await self._client.teams.post(new_team)
        team_id = result.id
        logger.info("Created Team '%s' — ID: %s", team_name, team_id)

        # Wait briefly for provisioning
        await asyncio.sleep(5)
        return team_id

    async def ensure_channel(
        self,
        team_id: str,
        channel_name: str = CHANNEL_NAME,
    ) -> str:
        """Create the channel inside the Team if it doesn't exist. Returns channel ID."""
        channels = await self._client.teams.by_team_id(team_id).channels.get()

        if channels and channels.value:
            for ch in channels.value:
                if ch.display_name == channel_name:
                    logger.info(
                        "Channel '%s' already exists — ID: %s",
                        channel_name,
                        ch.id,
                    )
                    return ch.id

        # Create the channel
        new_channel = Channel(
            display_name=channel_name,
            description=CHANNEL_DESCRIPTION,
        )
        result = await self._client.teams.by_team_id(team_id).channels.post(
            new_channel
        )
        logger.info("Created channel '%s' — ID: %s", channel_name, result.id)
        return result.id

    async def install_bot_in_team(self, team_id: str, bot_app_id: str) -> None:
        """Install a bot (by its Entra app ID) in the Team.

        Requires the bot to be published in the org's app catalog or
        side-loaded.  This step can also be done manually via Teams Admin.
        """
        try:
            installation = TeamsAppInstallation()
            # The app catalog ID format for custom bots
            installation.additional_data = {
                "teamsApp@odata.bind": (
                    f"https://graph.microsoft.com/v1.0/"
                    f"appCatalogs/teamsApps/{bot_app_id}"
                ),
            }
            await self._client.teams.by_team_id(
                team_id
            ).installed_apps.post(installation)
            logger.info("Installed bot %s in team %s", bot_app_id, team_id)
        except Exception as e:
            logger.warning(
                "Could not install bot in team (may need manual install "
                "or app catalog publishing): %s",
                e,
            )

    async def close(self) -> None:
        await self._credential.close()
