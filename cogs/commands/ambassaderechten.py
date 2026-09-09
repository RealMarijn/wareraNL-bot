"""Slash command /ambassaderechten — geeft President/VP/MoFA "Manage Channels"
op de ambassade-achtige categorieën, en houdt dat automatisch zo.

Production guild only — these are hardcoded production category/role IDs
(three of the four category IDs are also config["channels"]["embassy_categories"];
the fourth isn't currently in that config list but gets the same treatment
here since it was named alongside the others in the request).

The channels inside these categories were never "Synced to Category", so
each channel has its own explicit (and here, incomplete) permission
overwrites rather than inheriting from the category — granting the
category alone doesn't reach any existing channel in it. This does two
things:
  1. /ambassaderechten — one-off admin command that walks the categories AND
     every channel currently inside them, granting manage_channels to
     President/VP/MoFA on each (backfill for what already exists).
  2. on_guild_channel_create — going forward, any new channel created inside
     one of these categories gets the same three grants automatically,
     whether it was created by the bot's own embassy flow (cogs/welcome.py)
     or manually by an admin.

Grants are additive per role (channel.overwrites_for(role) is read first and
only manage_channels is added onto it, then written back via overwrite=)
rather than a blind set_permissions(role, manage_channels=True) — the kwarg
form replaces a role's ENTIRE overwrite on that channel with just the kwargs
given, which would silently wipe any other permission already set for that
role there.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

# Three of these four are config["channels"]["embassy_categories"]; the
# fourth (1496129836954157106) isn't, but was named alongside the others.
_TARGET_CATEGORY_IDS: tuple[int, ...] = (
    1496129836954157106,
    1456259898349453433,
    1481707074445381723,
    1496021951720980681,
)

_ROLE_CONFIG_KEYS: tuple[str, ...] = ("president", "vice_president", "minister_foreign_affairs")

_GRANT_REASON = "ambassaderechten: President/VP/MoFA kanaalbeheer"


def _target_role_ids(roles_cfg: dict) -> list[int]:
    return [rid for key in _ROLE_CONFIG_KEYS if (rid := roles_cfg.get(key))]


async def _grant_manage_channels(
    channel: discord.abc.GuildChannel, role: discord.Role
) -> bool:
    """Add manage_channels=True for *role* on *channel*, preserving whatever
    else is already set for that role there. Returns True if a change was
    actually made (False if it already had the permission — keeps repeat
    runs/creations cheap and out of the audit log when nothing changed)."""
    current = channel.overwrites_for(role)
    if current.manage_channels is True:
        return False
    current.manage_channels = True
    await channel.set_permissions(role, overwrite=current, reason=_GRANT_REASON)
    return True


class AmbassadeRechtenCog(CommandCogBase, name="ambassaderechten"):
    """Slash command + listener: President/VP/MoFA manage_channels on the
    ambassade-achtige categorieën, backfilled and kept up to date."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def _production_guild_id(self) -> int:
        return int(self.config.get("guild_id") or 0)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if channel.guild.id != self._production_guild_id():
            return
        category_id = getattr(channel, "category_id", None)
        if category_id not in _TARGET_CATEGORY_IDS:
            return

        roles_cfg = self.config.get("roles", {})
        role_ids = _target_role_ids(roles_cfg)
        if not role_ids:
            return

        granted = 0
        for role_id in role_ids:
            role = channel.guild.get_role(role_id)
            if not role:
                continue
            try:
                if await _grant_manage_channels(channel, role):
                    granted += 1
            except discord.Forbidden:
                logger.warning(
                    "ambassaderechten: no permission to update new channel %r for role %s",
                    channel.name, role_id,
                )
        if granted:
            logger.info(
                "ambassaderechten: granted manage_channels to %d role(s) on new channel %r",
                granted, channel.name,
            )

    @app_commands.command(
        name="ambassaderechten",
        description="Geef President/VP/MoFA kanaalbeheer op de ambassade-categorieën (en alle kanalen erin).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ambassaderechten(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.guild.id != self._production_guild_id():
            await interaction.response.send_message(
                "❌ Dit commando is alleen beschikbaar op de officiële server.",
                ephemeral=True,
            )
            return

        roles_cfg = self.config.get("roles", {})
        role_ids = _target_role_ids(roles_cfg)
        roles = [r for rid in role_ids if (r := interaction.guild.get_role(rid))]
        if not roles:
            await interaction.response.send_message(
                "❌ Kon President/VP/MoFA rollen niet vinden op deze server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        categories_done = 0
        channels_done = 0
        grants_made = 0
        missing_categories: list[int] = []

        for category_id in _TARGET_CATEGORY_IDS:
            category = interaction.guild.get_channel(category_id)
            if not isinstance(category, discord.CategoryChannel):
                missing_categories.append(category_id)
                continue
            categories_done += 1

            targets: list[discord.abc.GuildChannel] = [category, *category.channels]
            for target in targets:
                channels_done += 1
                for role in roles:
                    try:
                        if await _grant_manage_channels(target, role):
                            grants_made += 1
                    except discord.Forbidden:
                        logger.warning(
                            "ambassaderechten: no permission to update %r for role %s",
                            target.name, role.id,
                        )

        lines = [
            f"✅ **{categories_done}/{len(_TARGET_CATEGORY_IDS)}** categorieën verwerkt "
            f"({channels_done} categorieën + kanalen in totaal).",
            f"• Nieuwe rechten toegevoegd: **{grants_made}** "
            f"(al aanwezige rechten zijn overgeslagen).",
            f"• Rollen: {', '.join(r.mention for r in roles)}",
        ]
        if missing_categories:
            lines.append(
                "⚠️ Niet gevonden: " + ", ".join(str(cid) for cid in missing_categories)
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(AmbassadeRechtenCog(bot))
