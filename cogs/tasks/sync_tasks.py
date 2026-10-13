"""Automated daily sync tasks:

1. Daily commander role check — grant/revoke the commandant role based on
   mu.getById API data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase
from cogs.tasks.war_guild_divisions import DIVISION_MUS
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

# ── Cooldown intervals ──────────────────────────────────────────────────────
_DAILY_H = 24

# Marijn's Discord user ID (bot owner, bypasses the role check below)
_MARIJN_DISCORD_ID = 565626197048819731


# ── Helpers ─────────────────────────────────────────────────────────────────

def _unwrap_mu_getbyid(resp: Any) -> dict | None:
    """Unwrap tRPC response from /mu.getById and return the data dict."""
    if not isinstance(resp, dict):
        return None
    for outer in (resp, resp.get("result", {})):
        if isinstance(outer, dict):
            data = outer.get("data")
            if isinstance(data, dict) and "_id" in data:
                return data
    return None


class SyncTasks(TaskCogBase, name="sync_tasks"):
    """Daily automated sync tasks + manual slash commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.sync_loop.start()

    def cog_unload(self) -> None:
        self.sync_loop.cancel()

    # ── Main hourly tick ────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def sync_loop(self) -> None:
        now = datetime.now(timezone.utc)

        for name, key, interval_h, skip_in_testing, coro in [
            (
                "commander_role_check",
                "commander_role_check_last_run",
                _DAILY_H,
                False,
                self._check_commander_roles,
            ),
        ]:
            if not self._db:
                continue
            # Skip tasks that are disabled in testing mode
            if skip_in_testing and getattr(self.bot, "testing", False):
                continue
            try:
                last_str = await self._db.get_poll_state(key)
                if last_str:
                    elapsed_h = (
                        now - datetime.fromisoformat(last_str)
                    ).total_seconds() / 3600
                    if elapsed_h < interval_h:
                        continue
            except Exception:
                logger.exception("sync_loop: failed reading poll state %s", key)

            try:
                await coro()
            except Exception:
                logger.exception("sync_loop: %s failed", name)

            try:
                await self._db.set_poll_state(key, now.isoformat())
            except Exception:
                logger.exception("sync_loop: failed writing poll state %s", key)

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self._wait_for_services()

    # ════════════════════════════════════════════════════════════════════════
    # Task 1 — Commander (commandant) role check
    # ════════════════════════════════════════════════════════════════════════

    async def _check_commander_roles(self) -> dict[str, int]:
        """Grant/revoke the commandant role based on mu.getById API data."""
        stats = {
            "mus_checked": 0,
            "commanders_found": 0,
            "added": 0,
            "removed": 0,
            "errors": 0,
        }
        if not self._db or not self._client:
            return stats

        # Look up MU IDs for all division MUs from the known_mus registry
        all_mu_names: list[str] = [
            name for names in DIVISION_MUS.values() for name in names
        ]
        tracked_mu_ids: list[str] = []
        for mu_name in all_mu_names:
            mu_id, _ = await self._db.get_known_mu_by_name(mu_name)
            if mu_id:
                tracked_mu_ids.append(mu_id)
        if not tracked_mu_ids:
            logger.warning(
                "commander_role_check: no MU IDs found in known_mus for division MUs"
            )
            return stats

        commandant_role_id: int | None = None
        try:
            commandant_role_id = int(
                self.bot.config.get("roles", {}).get("commandant", 0) or 0
            )
        except (TypeError, ValueError):
            commandant_role_id = None

        if not commandant_role_id:
            logger.warning("commander_role_check: commandant role ID not configured")
            return stats

        # Collect all in-game commander IDs across tracked MUs
        current_commanders: set[str] = set()  # in_game_user_ids

        for mu_id in tracked_mu_ids:
            try:
                resp = await self._client.get(
                    "/mu.getById",
                    params={"input": json.dumps({"muId": mu_id})},
                )
                mu_data = _unwrap_mu_getbyid(resp)
                if not mu_data:
                    continue
                stats["mus_checked"] += 1
                commanders: list[str] = mu_data.get("roles", {}).get("commanders", [])
                # get managers (owner) and append
                commanders.extend(mu_data.get("roles", {}).get("managers", []))
                current_commanders.update(str(c) for c in commanders if c)
            except Exception:
                logger.exception(
                    "commander_role_check: failed fetching mu %s", mu_id
                )

        # In testing mode, merge in fake commanders defined in config
        testing = bool(getattr(self.bot, "testing", False))
        if testing:
            fake_commanders = self.bot.config.get("testing_commanders", [])
            current_commanders.update(str(c) for c in fake_commanders if c)
            if fake_commanders:
                logger.info(
                    "commander_role_check: testing mode — added %d fake commanders: %s",
                    len(fake_commanders),
                    fake_commanders,
                )

        stats["commanders_found"] = len(current_commanders)

        # If the API returned no data at all, assume it is down and abort so
        # that we never accidentally strip Commander roles due to an outage.
        if stats["mus_checked"] == 0:
            logger.warning(
                "commander_role_check: no MUs could be fetched from the API "
                "(API may be down) — skipping role changes to avoid mass-removal"
            )
            return stats

        production_guild_id = str(self.config.get("guild_id", ""))
        for guild in self.bot.guilds:
            if str(guild.id) == production_guild_id:
                continue
            guild_id = str(guild.id)
            commandant_role = guild.get_role(commandant_role_id)
            if commandant_role is None:
                continue

            # Build in_game → discord mapping for all linked users
            try:
                links = await self._db.get_identity_links_for_guild(guild_id)
            except Exception:
                logger.exception(
                    "commander_role_check: failed loading links for guild %s", guild.id
                )
                continue

            ingame_to_discord: dict[str, str] = {
                str(link["in_game_user_id"]): str(link["discord_user_id"])
                for link in links
                if link.get("in_game_user_id") and link.get("discord_user_id")
            }
            discord_to_ingame: dict[str, str] = {v: k for k, v in ingame_to_discord.items()}

            # Current holder set (members who already have commandant role)
            current_holders: set[discord.Member] = {
                m for m in guild.members if commandant_role in m.roles and not m.bot
            }

            # Grant role to commanders not yet holding it
            for in_game_id in current_commanders:
                discord_id = ingame_to_discord.get(in_game_id)
                if not discord_id or not discord_id.isdigit():
                    continue
                member = guild.get_member(int(discord_id))
                if member is None or member.bot:
                    continue
                if commandant_role in member.roles:
                    continue
                try:
                    await member.add_roles(
                        commandant_role,
                        reason="Commandant rol sync op basis van MU API data",
                    )
                    stats["added"] += 1
                except discord.HTTPException:
                    stats["errors"] += 1

            # Revoke role from members who are no longer commanders
            for member in current_holders:
                discord_id = str(member.id)
                in_game_id = discord_to_ingame.get(discord_id)
                if in_game_id and in_game_id in current_commanders:
                    continue  # still a commander
                try:
                    await member.remove_roles(
                        commandant_role,
                        reason="Commandant rol sync — niet meer commandant",
                    )
                    stats["removed"] += 1
                except discord.HTTPException:
                    stats["errors"] += 1

        logger.info(
            "commander_role_check: done (mus=%d commanders=%d added=%d removed=%d errors=%d)",
            stats["mus_checked"],
            stats["commanders_found"],
            stats["added"],
            stats["removed"],
            stats["errors"],
        )
        return stats

    # ════════════════════════════════════════════════════════════════════════
    # Slash commands — manual triggers
    # ════════════════════════════════════════════════════════════════════════

    def _is_privileged(self, interaction: discord.Interaction) -> bool:
        """Return True if the invoker is the bot owner or has a privileged role."""
        if getattr(self.bot, "testing", False):
            return True
        if interaction.user.id == _MARIJN_DISCORD_ID:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        privileged_keys = {"officier", "government", "commandant"}
        role_ids = {
            self.bot.config.get("roles", {}).get(k)
            for k in privileged_keys
        }
        return any(r.id in role_ids for r in interaction.user.roles)

    @app_commands.command(
        name="commandant-check",
        description="Sync de commandant rol op basis van MU API data.",
    )
    @has_privileged_role()
    async def cmd_commandant_check(self, interaction: discord.Interaction) -> None:
        if not self._is_privileged(interaction):
            await interaction.response.send_message(
                "Je hebt geen toegang tot dit commando.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🎖️ Commandant check gestart...", ephemeral=True
        )
        try:
            stats = await self._check_commander_roles()
        except Exception as exc:
            logger.exception("cmd_commandant_check: failed")
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Commandant check klaar — MU's gecontroleerd: **{stats['mus_checked']}**, "
            f"commandanten gevonden: **{stats['commanders_found']}**, "
            f"toegevoegd: **{stats['added']}**, verwijderd: **{stats['removed']}**, "
            f"fouten: **{stats['errors']}**.",
            ephemeral=True,
        )
        if self._db:
            await self._db.set_poll_state(
                "commander_role_check_last_run", datetime.now(timezone.utc).isoformat()
            )


async def setup(bot) -> None:
    """Add the SyncTasks cog to the bot."""
    await bot.add_cog(SyncTasks(bot))
