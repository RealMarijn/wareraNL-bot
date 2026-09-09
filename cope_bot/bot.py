"""COPErator — the COPE alliance Discord verification bot.

Handles WarEra<->Discord verification (ticket-based, admin-approved) and a
periodic nickname/country-role sync for the COPE alliance server.
Run with: python -m cope_bot.bot

Required environment variables:
  TOKEN_COPE   Discord bot token for this application.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands

# Allow importing from the project root (services/, etc.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cope_bot")


class CopeBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Needed for guild.get_member/fetch_member and nickname/role edits
        # (ticket approval, /link, the periodic sync loop). No on_message
        # listener anywhere in cope_bot, so message_content isn't needed —
        # one less privileged intent to have to enable in the portal.
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self._reconciled_deletions = False

    async def setup_hook(self) -> None:
        from cope_bot.db import open_db
        self.cope_db = await open_db()

        import cope_bot.cog as cog_module
        await self.add_cog(cog_module.VerificationCog(self, self.cope_db))

        # Register persistent views so buttons survive restarts.
        from cope_bot.cog import TicketActionView, VerificationView
        self.add_view(VerificationView())
        self.add_view(TicketActionView())

        # Sync slash commands to the COPE guild only. This 403s until the bot
        # has actually been invited to that guild — don't let that take the
        # whole process down with it (the aiosqlite connection opened above
        # spawns a non-daemon worker thread, which would otherwise keep the
        # container hanging as a zombie instead of exiting so
        # `restart: unless-stopped` can retry); log it clearly and continue.
        # The bot still comes online normally — slash commands just won't
        # appear in the COPE guild until the next restart after being invited.
        from cope_bot.cog import GUILD_ID
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %d", GUILD_ID)
        except discord.Forbidden:
            logger.warning(
                "Could not sync commands to guild %d — the bot hasn't been "
                "invited there yet (or is missing the applications.commands "
                "scope). Invite it, then restart this container.", GUILD_ID,
            )

    async def on_ready(self) -> None:
        logger.info("COPE bot ready — logged in as %s", self.user)
        # on_ready can fire again on gateway reconnects — only resume pending
        # ticket deletions once per process, not on every reconnect.
        if not self._reconciled_deletions:
            self._reconciled_deletions = True
            from cope_bot.cog import _reconcile_pending_deletions
            await _reconcile_pending_deletions(self, self.cope_db)


def main() -> None:
    token = os.environ.get("TOKEN_COPE")
    if not token:
        logger.error("TOKEN_COPE environment variable not set")
        sys.exit(1)

    bot = CopeBot()
    try:
        asyncio.run(bot.start(token))
    except Exception:
        logger.exception("COPE bot crashed")
        # aiosqlite's connection thread is non-daemon, so an unhandled crash
        # here would otherwise leave a zombie process running forever instead
        # of actually exiting — os._exit forces the interpreter down so
        # Docker's restart policy can do its job.
        os._exit(1)


if __name__ == "__main__":
    main()
