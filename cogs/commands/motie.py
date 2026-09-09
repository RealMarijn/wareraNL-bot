"""Slash command /motie — genereert een motie-sjabloon voor het congres-/regeringskanaal.

Production guild + war guild (config["guild_id"] and config["war_guild"]["guild_id"])
— not nigeria-bot, a separate process entirely. The production guild is the
real target (that's where the actual congress is); the war guild is allowed
too purely so it's easier to test there. Since the main bot process serves
both guilds at once (see cogs/tasks/war_sync.py), this can't rely on
Discord's own guild-scoped command sync the way cogs/owner.py's admin-only
commands do; it's a normal global command gated by a runtime check against
interaction.guild.id instead.

Restricted to congress-adjacent roles (congreslid/government/president/
vice_president — the same set cogs/commands/samenvatting.py already uses
for "samenvatting_allowed"-equivalent access) or server admins, matching
"congress members" from the feature request.

Opens a modal (Discord's UI can't mix a slash command's own options with a
modal beyond simple booleans — the who-to-tag choice is taken as command
options *before* the modal opens, since interaction.response.send_modal()
must be the very first response) collecting the free-text parts of the
template, then replies with the assembled motion text as one or more
ephemeral messages (Discord's 2000-char message cap can be smaller than the
combined template) for the user to copy, tweak, and post themselves — the
bot never posts a motion into a channel on anyone's behalf.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from cogs.commands._congress_templates import (
    allowed_guild_ids,
    format_openbaarheid,
    is_congress_member,
    role_mention,
    send_template_chunks,
)

logger = logging.getLogger("discord_bot")


class MotieModal(discord.ui.Modal, title="Nieuwe motie"):
    def __init__(self, *, tag_line: str, indiener: str) -> None:
        super().__init__()
        self._tag_line = tag_line
        self._indiener = indiener

        self.titel = discord.ui.TextInput(
            label="Titel van de motie",
            style=discord.TextStyle.short,
            max_length=100,
            placeholder="Bijv. Handelsverdrag met Noorwegen",
        )
        self.openbaarheid = discord.ui.TextInput(
            label="Openbaarheidsstatus",
            style=discord.TextStyle.short,
            max_length=100,
            placeholder='"0" = direct openbaar, "3" = na 3 dagen, of "C: reden" = conditioneel',
        )
        self.onderwerp = discord.ui.TextInput(
            label="Onderwerp (kort)",
            style=discord.TextStyle.short,
            max_length=200,
        )
        self.motie_tekst = discord.ui.TextInput(
            label="Motie (uitgebreide toelichting)",
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.extra = discord.ui.TextInput(
            label="Stappenplan / Motivatie (optioneel)",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
            placeholder="Bijv. een genummerd stappenplan en/of de motivatie voor dit voorstel",
        )

        for item in (
            self.titel, self.openbaarheid, self.onderwerp, self.motie_tekst, self.extra,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parts = [
            self._tag_line,
            f"# Motie *{self.titel}*",
            "## Openbaarheidsstatus",
            format_openbaarheid(str(self.openbaarheid)),
            "## Indiener",
            self._indiener,
            "## Onderwerp",
            str(self.onderwerp).strip(),
            "## Motie",
            str(self.motie_tekst).strip(),
        ]
        extra_value = str(self.extra).strip()
        if extra_value:
            parts.append("## Stappenplan / Motivatie (optioneel)")
            parts.append(extra_value)

        await send_template_chunks(interaction, "\n".join(parts))


class MotieCog(CommandCogBase, name="motie"):
    """Slash command /motie — motie-sjabloon generator voor het congres."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="motie",
        description="Maak een motie-sjabloon aan voor het congres-/regeringskanaal.",
    )
    @app_commands.describe(
        president="Tag ook de President.",
        vicepresident="Tag ook de Vice-President.",
        ministers="Tag ook de Regering (ministers).",
    )
    async def motie(
        self,
        interaction: discord.Interaction,
        president: bool = False,
        vicepresident: bool = False,
        ministers: bool = False,
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
                "kunnen een motie aanmaken.",
                ephemeral=True,
            )
            return

        mentions = [role_mention(self.config, "congreslid")]
        if president:
            mentions.append(role_mention(self.config, "president"))
        if vicepresident:
            mentions.append(role_mention(self.config, "vice_president"))
        if ministers:
            mentions.append(role_mention(self.config, "government"))
        tag_line = " ".join(m for m in mentions if m) or "@Congreslid"

        await interaction.response.send_modal(
            MotieModal(tag_line=tag_line, indiener=member.mention)
        )


async def setup(bot) -> None:
    await bot.add_cog(MotieCog(bot))
