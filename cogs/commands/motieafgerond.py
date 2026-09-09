"""Slash command /motieafgerond — genereert de afsluitmelding voor een
motie waarvan de stemming klaar is.

Same production+war-guild / congress-role gating as /motie — see
cogs/commands/motie.py's docstring. All fields are short/single-line or a
fixed choice, so — like /stembureauarchiefpost — there's no modal, just
command options.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from cogs.commands._congress_templates import (
    allowed_guild_ids,
    is_congress_member,
    send_template_chunks,
)

logger = logging.getLogger("discord_bot")


class MotieAfgerondCog(CommandCogBase, name="motieafgerond"):
    """Slash command /motieafgerond — afsluitmelding-sjabloon generator."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="motieafgerond",
        description="Meld dat de stemming over een motie is afgerond.",
    )
    @app_commands.describe(
        titel="Titel van de motie.",
        uitslag="Is de motie aangenomen of geweigerd?",
        stemronde_link="Discord link naar de #stemronde post (rechtermuisknop > Copy Message Link).",
        stemronde_archief_link="Discord link naar de #stemronde-archief post (rechtermuisknop > Copy Message Link).",
        opmerking="Optionele opmerking, bijv. 'Ga gerust door met debatteren en verder uitwerken van het plan.'",
    )
    @app_commands.choices(
        uitslag=[
            app_commands.Choice(name="Aangenomen", value="aangenomen"),
            app_commands.Choice(name="Geweigerd", value="geweigerd"),
        ]
    )
    async def motieafgerond(
        self,
        interaction: discord.Interaction,
        titel: str,
        uitslag: app_commands.Choice[str],
        stemronde_link: str,
        stemronde_archief_link: str,
        opmerking: Optional[str] = None,
    ) -> None:
        if not interaction.guild or interaction.guild.id not in allowed_guild_ids(self.config):
            await interaction.response.send_message(
                "❌ Dit commando is hier niet beschikbaar.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not is_congress_member(self.config, member):
            await interaction.response.send_message(
                "❌ Alleen congresleden (of regering/president/vicepresident) "
                "kunnen een motie afronden.",
                ephemeral=True,
            )
            return

        parts = [
            f"# :lock_with_ink_pen: Motie *{titel}* is afgerond en deze motie is {uitslag.value}",
            f"-# Stemronde: {stemronde_link}",
            f"-# Stemronde archief: {stemronde_archief_link}",
        ]
        if opmerking and opmerking.strip():
            parts.append(opmerking.strip())
        await send_template_chunks(interaction, "\n".join(parts))


async def setup(bot) -> None:
    await bot.add_cog(MotieAfgerondCog(bot))
