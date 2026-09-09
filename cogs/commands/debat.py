"""Slash command /debat — genereert de startpost-inhoud voor een nieuwe
draad in #debatruimtes.

Same production+war-guild / congress-role gating as /motie — see
cogs/commands/motie.py's docstring. Shows an explanatory message (how to
actually start the Discord thread — the bot doesn't do that part, since
"New Post" in a forum channel isn't something a bot can trigger on a
user's behalf) with an "open form" button, then a single 5-field modal —
that's the full field count (who-else-to-tag plus the 4 template sections),
so unlike /motie/​/stembureaupost this doesn't need a second chained modal.
See cogs/commands/_congress_templates.py for why the description is a
preceding message rather than in-modal text, and why "who else to tag" is a
free-text field rather than command-option booleans (a pure-modal command
has no command options at all).
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from cogs.commands._congress_templates import (
    OpenFormView,
    allowed_guild_ids,
    build_tag_line,
    is_congress_member,
    send_template_chunks,
)

logger = logging.getLogger("discord_bot")

_DESCRIPTION = (
    "In de Discord channel #debatruimtes kun je rechtsboven op 'New Post' klikken om een "
    "nieuwe thread te starten. De titel van de thread graag starten met 'DEBAT: ' met daarna "
    "het onderwerp, bijvoorbeeld: \"DEBAT: Verhogen congreslid salaris\" — met daarna de "
    "volgende startpost inhoud:\n\n"
    "Klik hieronder om het formulier voor die startpost-inhoud te openen."
)


class DebatModal(discord.ui.Modal, title="Nieuw debat"):
    def __init__(self, *, config: dict) -> None:
        super().__init__()
        self._config = config

        self.tag_extra = discord.ui.TextInput(
            label="Extra taggen (optioneel)",
            style=discord.TextStyle.short,
            max_length=100,
            required=False,
            placeholder="Bijv. president, vicepresident, ministers",
        )
        self.context = discord.ui.TextInput(
            label="Context / situatie",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            placeholder="Korte uitleg van de situatie of aanleiding voor het debat.",
        )
        self.vraag = discord.ui.TextInput(
            label="Vraag / probleemstelling",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            placeholder="Wat moet het congres in dit debat bepalen of bespreken?",
        )
        self.voorstellen = discord.ui.TextInput(
            label="Voorstel(len) / Mogelijke richtingen",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            placeholder="Wat stel je voor? Wat zijn de opties?",
        )
        self.doel = discord.ui.TextInput(
            label="Doel van debat",
            style=discord.TextStyle.paragraph,
            max_length=600,
            placeholder="Richting bepalen, draagvlak peilen, informatie verzamelen, ...",
        )

        for item in (self.tag_extra, self.context, self.vraag, self.voorstellen, self.doel):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tag_line = build_tag_line(self._config, str(self.tag_extra))
        parts = [
            tag_line,
            "# Context / situatie",
            str(self.context).strip(),
            "# Vraag / probleemstelling",
            str(self.vraag).strip(),
            "# Voorstel(len) / Mogelijke richtingen",
            str(self.voorstellen).strip(),
            "# Doel van debat",
            str(self.doel).strip(),
        ]
        await send_template_chunks(interaction, "\n".join(parts))


class DebatCog(CommandCogBase, name="debat"):
    """Slash command /debat — startpost-sjabloon generator voor #debatruimtes."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="debat",
        description="Maak de startpost-inhoud voor een nieuw debat in #debatruimtes.",
    )
    async def debat(self, interaction: discord.Interaction) -> None:
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
                "kunnen een debat starten.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            content=_DESCRIPTION,
            view=OpenFormView(lambda: DebatModal(config=self.config)),
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(DebatCog(bot))
