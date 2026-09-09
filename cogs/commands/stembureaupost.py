"""Slash command /stembureaupost — genereert een stemronde-post voor een
aangenomen motie/petitie, klaar om in het stemkanaal te plaatsen.

Same production+war-guild / congress-role gating as /motie — see
cogs/commands/motie.py's docstring. Always tags Congreslid + President +
Vice-President (unlike /motie/​/debat, that trio isn't optional here — a
vote concerns the whole congress plus presidential oversight by design).

Pure modal, no command options — see cogs/commands/motie.py's docstring for
why. This template has 8 variable spots (titel, debat-link, openbaarheid,
onderwerp, motie, stappenplan, and the two stem-toelichtingen), so it's
split across two chained modals of 4 fields each: the first collects the
short/single-line fields, its on_submit opens a second modal for the
free-text paragraphs, and THAT modal's on_submit assembles and sends the
final text. The "## Stemming" tally section is intentionally left as a
literal placeholder (counts + closing date aren't known yet at creation
time) — filled in by hand once the vote closes, same as in the template
this mirrors.
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


class StembureauPostModal2(discord.ui.Modal, title="Nieuwe stemronde (2/2)"):
    def __init__(
        self, *, tag_line: str, titel: str, debat_link: str, openbaarheid_line: str, onderwerp: str,
    ) -> None:
        super().__init__()
        self._tag_line = tag_line
        self._titel = titel
        self._debat_link = debat_link
        self._openbaarheid_line = openbaarheid_line
        self._onderwerp = onderwerp

        self.motie_tekst = discord.ui.TextInput(
            label="Motie (uitgebreide toelichting)",
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.stappenplan = discord.ui.TextInput(
            label="Stappenplan",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            placeholder="1. ...\n2. ...\n3. ...",
        )
        self.akkoord_reden = discord.ui.TextInput(
            label="Akkoord betekent...",
            style=discord.TextStyle.paragraph,
            max_length=300,
            placeholder="Waarop stemt men voor akkoord?",
        )
        self.niet_akkoord_reden = discord.ui.TextInput(
            label="Niet akkoord betekent...",
            style=discord.TextStyle.paragraph,
            max_length=300,
            placeholder="Waarop stemt men voor niet akkoord?",
        )

        for item in (
            self.motie_tekst, self.stappenplan, self.akkoord_reden, self.niet_akkoord_reden,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parts = [
            self._tag_line,
            f"# :ballot_box: Motie *{self._titel}*",
            f"-# Ontstaan vanuit {self._debat_link}",
            "## Openbaarheidsstatus",
            self._openbaarheid_line,
            "## Indiener",
            interaction.user.mention,
            "## Onderwerp",
            self._onderwerp,
            "## Motie",
            str(self.motie_tekst).strip(),
            "## Stappenplan",
            str(self.stappenplan).strip(),
            "## Stemopties",
            f":white_check_mark: Akkoord – {str(self.akkoord_reden).strip()}",
            f":ballot_box_with_check: Akkoord, maar met aanpassingen (zie opmerking in {self._debat_link}).",
            ":white_circle: Onthouden van stemmen",
            f":x: Niet akkoord – {str(self.niet_akkoord_reden).strip()}",
            "*Stemming loopt tot een meerderheid van 18 stemmen, bij onthoudingen verandert "
            "de benodigde meerderheid naar verhouding; stem wijzigen of onthouden mag tot "
            "sluiting (35 stemgerechtigden, 50% is 17,5).*",
            "Na afronding wordt de stemming gesloten en de stemopties aangepast naar de "
            "uitgebrachte stemmen",
            "## Stemming",
            "(Aantal):white_check_mark: Akkoord",
            "(Aantal):ballot_box_with_check: Akkoord, maar met aanpassingen",
            "(Aantal):white_circle: Onthouden van stemmen",
            "(Aantal):x: Niet akkoord",
            "Gesloten op datum dd-mm-jj uu:mm",
        ]
        await send_template_chunks(interaction, "\n".join(parts))


class StembureauPostModal1(discord.ui.Modal, title="Nieuwe stemronde (1/2)"):
    def __init__(self, *, config: dict) -> None:
        super().__init__()
        self._config = config

        self.titel = discord.ui.TextInput(
            label="Titel van de motie",
            style=discord.TextStyle.short,
            max_length=100,
        )
        self.debat_link = discord.ui.TextInput(
            label="Link naar debat / staten-generaal post",
            style=discord.TextStyle.short,
            max_length=200,
            placeholder="Rechtermuisknop op het bericht > Copy Message Link",
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
        for item in (self.titel, self.debat_link, self.openbaarheid, self.onderwerp):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        mentions = [
            role_mention(self._config, "congreslid"),
            role_mention(self._config, "president"),
            role_mention(self._config, "vice_president"),
        ]
        tag_line = " ".join(m for m in mentions if m) or "@Congreslid @President @Vice-President"

        await interaction.response.send_modal(
            StembureauPostModal2(
                tag_line=tag_line,
                titel=str(self.titel).strip(),
                debat_link=str(self.debat_link).strip(),
                openbaarheid_line=format_openbaarheid(str(self.openbaarheid)),
                onderwerp=str(self.onderwerp).strip(),
            )
        )


class StembureauPostCog(CommandCogBase, name="stembureaupost"):
    """Slash command /stembureaupost — stemronde-sjabloon generator."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="stembureaupost",
        description="Start een stemronde voor een motie/petitie in het stemkanaal.",
    )
    async def stembureaupost(self, interaction: discord.Interaction) -> None:
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
                "kunnen een stemronde starten.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(StembureauPostModal1(config=self.config))


async def setup(bot) -> None:
    await bot.add_cog(StembureauPostCog(bot))
