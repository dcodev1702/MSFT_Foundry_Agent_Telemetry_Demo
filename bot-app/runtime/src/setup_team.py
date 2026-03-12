#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════════
# setup_team.py — One-time bootstrap: create the operational Team
# and channel in Microsoft Teams via the Graph API.
#
# Usage:
#   python setup_team.py
#   python setup_team.py --bot-app-id <teams-app-manifest-id>
#
# Prerequisites:
#   - Azure CLI authenticated (az login) OR Managed Identity
#   - Graph application permissions: Group.ReadWrite.All, Team.Create
#   - Admin consent granted for the above permissions
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from graph_setup import GraphTeamsSetup, TEAM_NAME, CHANNEL_NAME


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main(bot_app_id: str | None = None) -> None:
    setup = GraphTeamsSetup()

    try:
        logger.info("Ensuring Team '%s' exists …", TEAM_NAME)
        team_id = await setup.ensure_team()

        logger.info("Ensuring channel '%s' exists …", CHANNEL_NAME)
        channel_id = await setup.ensure_channel(team_id)

        if bot_app_id:
            logger.info("Installing bot app %s in the Team …", bot_app_id)
            await setup.install_bot_in_team(team_id, bot_app_id)

        print("\n" + "=" * 60)
        print("  Setup complete!")
        print(f"  Team ID    : {team_id}")
        print(f"  Channel ID : {channel_id}")
        print("=" * 60)

    finally:
        await setup.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bootstrap the Foundry Bot Team and channel in Microsoft Teams"
    )
    parser.add_argument(
        "--bot-app-id",
        default=os.getenv("TEAMS_APP_ID") or os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID"),
        help="Teams app manifest ID (to install in the Team)",
    )
    args = parser.parse_args()

    asyncio.run(main(bot_app_id=args.bot_app_id))
