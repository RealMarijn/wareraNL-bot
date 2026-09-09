"""Slash command /motieafgerond — genereert de afsluitmelding voor een
motie waarvan de stemming klaar is.

Same production+war-guild / congress-role gating as /motie — see
cogs/commands/motie.py's docstring. Pure modal, no command options — see
that same docstring for why. All 5 fields fit in one modal; "uitslag" is a
free-text field (not the app_commands.Choice dropdown this used to be,
since a modal only has TextInputs) normalized by normalize_uitslag —
anything not clearly "aangenomen"/"geweigerd" is passed through as typed
rather than rejected, same forgiving philosophy as format_openbaarheid.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from cogs.commands._congress_templates import (
    allowed_guild_ids,
    is_congress_member,
    normalize_uitslag,
    send_template_chunks,
)

logger = logging.getLogger("discord_bot")


class MotieAfgerondModal(discord.ui.Modal, title="Motie afgerond"):
    titel = discord.ui.TextInput(
        label="Titel van de motie",
        style=discord.TextStyle.short,
        max_length=100,
    )
    uitslag = discord.ui.TextInput(
        label="Uitslag",
        style=discord.TextStyle.short,
        max_length=30,
        placeholder='"aangenomen" of "geweigerd"',
    )
    stemronde_link = discord.ui.TextInput(
        label="Link naar #stemronde post",
        style=discord.TextStyle.short,
        max_length=200,
        placeholder="Rechtermuisknop op het bericht > Copy Message Link",
    )
    stemronde_archief_link = discord.ui.TextInput(
        label="Link naar #stemronde-archief post",
        style=discord.TextStyle.short,
        max_length=200,
        placeholder="Rechtermuisknop op het bericht > Copy Message Link",
    )
    opmerking = discord.ui.TextInput(
        label="Opmerking (optioneel)",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
        placeholder="Bijv. 'Ga gerust door met debatteren en verder uitwerken van het plan.'",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uitslag_value = normalize_uitslag(str(self.uitslag))
        parts = [
            f"# :lock_with_ink_pen: Motie *{str(self.titel).strip()}* is afgerond en deze motie is {uitslag_value}",
            f"-# Stemronde: {str(self.stemronde_link).strip()}",
            f"-# Stemronde archief: {str(self.stemronde_archief_link).strip()}",
        ]
        opmerking_value = str(self.opmerking).strip()
        if opmerking_value:
            parts.append(opmerking_value)
        await send_template_chunks(interaction, "\n".join(parts))


class MotieAfgerondCog(CommandCogBase, name="motieafgerond"):
    """Slash command /motieafgerond — afsluitmelding-sjabloon generator."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="motieafgerond",
        description="Meld dat de stemming over een motie is afgerond.",
    )
    async def motieafgerond(self, interaction: discord.Interaction) -> None:
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

        await interaction.response.send_modal(MotieAfgerondModal())


async def setup(bot) -> None:
    await bot.add_cog(MotieAfgerondCog(bot))
