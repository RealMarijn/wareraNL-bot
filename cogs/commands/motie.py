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

Pure modal, no command options — a Discord modal can't be pre-filled from
answers given elsewhere, so mixing slash-command options with a modal (the
original design) made it look like whatever was typed into the options got
thrown away the moment the modal opened. 6 fields total, split across two
chained modals since a single modal caps out at 5 (the short fields first,
including the "who else to tag" multi-select, then a second modal for the
two paragraph fields) — a modal submission's interaction can itself open
another modal as its first response, same as any other interaction. "Who
else to tag" is a Select (make_tag_select in _congress_templates.py) rather
than a TextInput — the closest thing to checkboxes a modal actually offers,
picking 0-3 of president/vice-president/ministers from a dropdown instead
of parsing free text. Replies with the assembled motion text as one or more
ephemeral, code-fenced messages (see _congress_templates.py) for the user
to copy, tweak, and post themselves — the bot never posts a motion into a
channel on anyone's behalf.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from cogs.commands._congress_templates import (
    allowed_guild_ids,
    build_tag_line_from_selection,
    format_openbaarheid,
    is_congress_member,
    make_tag_select,
    send_template_chunks,
)

logger = logging.getLogger("discord_bot")


class MotieModal2(discord.ui.Modal, title="Nieuwe motie (2/2)"):
    def __init__(self, *, tag_line: str, titel: str, openbaarheid_line: str, onderwerp: str) -> None:
        super().__init__()
        self._tag_line = tag_line
        self._titel = titel
        self._openbaarheid_line = openbaarheid_line
        self._onderwerp = onderwerp

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
        for item in (self.motie_tekst, self.extra):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parts = [
            self._tag_line,
            f"# Motie *{self._titel}*",
            "## Openbaarheidsstatus",
            self._openbaarheid_line,
            "## Indiener",
            interaction.user.mention,
            "## Onderwerp",
            self._onderwerp,
            "## Motie",
            str(self.motie_tekst).strip(),
        ]
        extra_value = str(self.extra).strip()
        if extra_value:
            parts.append("## Stappenplan / Motivatie (optioneel)")
            parts.append(extra_value)

        await send_template_chunks(interaction, "\n".join(parts))


class MotieModal1(discord.ui.Modal, title="Nieuwe motie (1/2)"):
    def __init__(self, *, config: dict) -> None:
        super().__init__()
        self._config = config

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
        self.tag_select = make_tag_select()
        for item in (self.titel, self.openbaarheid, self.onderwerp, self.tag_select):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tag_line = build_tag_line_from_selection(self._config, self.tag_select.values)
        await interaction.response.send_modal(
            MotieModal2(
                tag_line=tag_line,
                titel=str(self.titel).strip(),
                openbaarheid_line=format_openbaarheid(str(self.openbaarheid)),
                onderwerp=str(self.onderwerp).strip(),
            )
        )


class MotieCog(CommandCogBase, name="motie"):
    """Slash command /motie — motie-sjabloon generator voor het congres."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="motie",
        description="Maak een motie-sjabloon aan voor het congres-/regeringskanaal.",
    )
    async def motie(self, interaction: discord.Interaction) -> None:
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

        await interaction.response.send_modal(MotieModal1(config=self.config))


async def setup(bot) -> None:
    await bot.add_cog(MotieCog(bot))
