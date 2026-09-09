"""Slash command /stembureauarchiefpost — genereert de archief-samenvatting
van een afgesloten stemronde.

Same production+war-guild / congress-role gating as /motie — see
cogs/commands/motie.py's docstring. Pure modal, no command options — see
that same docstring for why. Every field here is short/single-line, so it
all fits in one modal (no chaining needed, unlike /motie/​/stembureaupost).
The Openbaarheidsstatus line is deliberately left as the literal "Kopieer
OS uit de motie post" instruction (not regenerated) since it needs to
exactly match what was already posted in the original /stembureaupost, and
the "## Stemming" tally is left as a placeholder for the same reason as in
/stembureaupost — filled in by hand once counted.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from cogs.commands._congress_templates import (
    allowed_guild_ids,
    is_congress_member,
    send_template_chunks,
)

logger = logging.getLogger("discord_bot")


class StembureauArchiefPostModal(discord.ui.Modal, title="Nieuwe archiefpost"):
    titel = discord.ui.TextInput(
        label="Titel van de motie",
        style=discord.TextStyle.short,
        max_length=100,
    )
    initiatiefnemer = discord.ui.TextInput(
        label="Initiatiefnemer",
        style=discord.TextStyle.short,
        max_length=100,
        placeholder="Speler/congreslid die de motie/petitie heeft ingediend",
    )
    debat_link = discord.ui.TextInput(
        label="Link naar debat / staten-generaal post",
        style=discord.TextStyle.short,
        max_length=200,
    )
    stemronde_link = discord.ui.TextInput(
        label="Link naar de stemronde-post",
        style=discord.TextStyle.short,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parts = [
            f"# :lock_with_ink_pen: Motie *{str(self.titel).strip()}*",
            "**Openbaarheidsstatus**",
            "Kopieer OS uit de motie post",
            f"> **Initiatiefnemer:** {str(self.initiatiefnemer).strip()}",
            f"> **Debat:** {str(self.debat_link).strip()}",
            f"> **Stemronde:** {str(self.stemronde_link).strip()}",
            "## Stemming",
            "(Aantal):white_check_mark: Akkoord",
            "(Aantal):ballot_box_with_check: Akkoord, maar met aanpassingen",
            "(Aantal):white_circle: Onthouden van stemmen",
            "(Aantal):x: Niet akkoord",
            "Gesloten op datum dd-mm-jjjj uu:mm",
        ]
        await send_template_chunks(interaction, "\n".join(parts))


class StembureauArchiefPostCog(CommandCogBase, name="stembureauarchiefpost"):
    """Slash command /stembureauarchiefpost — archiefpost-sjabloon generator."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="stembureauarchiefpost",
        description="Maak de archief-samenvatting van een afgesloten stemronde.",
    )
    async def stembureauarchiefpost(self, interaction: discord.Interaction) -> None:
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
                "kunnen een archiefpost aanmaken.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(StembureauArchiefPostModal())


async def setup(bot) -> None:
    await bot.add_cog(StembureauArchiefPostCog(bot))
