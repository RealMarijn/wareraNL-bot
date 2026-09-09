"""Slash command /stembureaupost — genereert een stemronde-post voor een
aangenomen motie/petitie, klaar om in het stemkanaal te plaatsen.

Same production+war-guild / congress-role gating as /motie — see
cogs/commands/motie.py's docstring. Always tags Congreslid + President +
Vice-President (unlike /motie/​/debat, that trio isn't optional here — a
vote concerns the whole congress plus presidential oversight by design).

Pure modal, no command options — see cogs/commands/motie.py's docstring for
why. This template has 8 variable spots (titel, debat-link, openbaarheid,
onderwerp, motie, stappenplan, and the two stem-toelichtingen), so it's two
modals of 4 fields each — but NOT chained directly. Chaining
(modal1.on_submit calling response.send_modal() for modal2) is technically
legal in discord.py but Discord's API rejects the payload discord.py 2.6.4
sends for a modal opened FROM a modal-submit interaction specifically
(confirmed live: HTTPException 400, "In type: Value must be one of {4, 5,
6, 7, 10, 12}" — see cogs/commands/motie.py's docstring for the full
diagnosis). So modal1's on_submit instead shows a short ephemeral
confirmation with a button (OpenFormView, same helper /debat uses for its
description step), and clicking THAT button opens modal2 — a button->modal
is a completely different, unaffected code path.

The majority-threshold sentence ("meerderheid van N stemmen... X
stemgerechtigden") is built from a live government.getByCountryId call
(fetch_congress_majority) rather than hardcoded — the Dutch congress's
member count changes with monthly elections, and the old hardcoded 18/35
had already gone stale (real count was 33 members / majority 17 when this
was fixed). The vote-tally section itself ("## Stemming" with (Aantal)
placeholders and a closing-date line) was dropped entirely per explicit
request — that's handled by /stembureauarchiefpost once the vote closes,
not needed in the opening post.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from cogs.commands._congress_templates import (
    OpenFormView,
    allowed_guild_ids,
    fetch_congress_majority,
    format_half,
    format_openbaarheid,
    is_congress_member,
    role_mention,
    send_template_chunks,
)

logger = logging.getLogger("discord_bot")


class StembureauPostModal2(discord.ui.Modal, title="Nieuwe stemronde (2/2)"):
    def __init__(
        self, *, tag_line: str, titel: str, debat_link: str, openbaarheid_line: str, onderwerp: str,
        client, config: dict,
    ) -> None:
        super().__init__()
        self._tag_line = tag_line
        self._titel = titel
        self._debat_link = debat_link
        self._openbaarheid_line = openbaarheid_line
        self._onderwerp = onderwerp
        self._client = client
        self._config = config

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
        majority_info = await fetch_congress_majority(self._client, self._config)
        if majority_info:
            total, majority = majority_info
            stemming_line = (
                f"*Stemming loopt tot een meerderheid van {majority} stemmen, bij onthoudingen "
                "verandert de benodigde meerderheid naar verhouding; stem wijzigen of onthouden "
                f"mag tot sluiting ({total} stemgerechtigden, 50% is {format_half(total)}).*"
            )
        else:
            stemming_line = (
                "*Stemming loopt tot een meerderheid van de stemgerechtigde congresleden "
                "(kon het actuele aantal niet live ophalen — check zelf hoeveel congresleden "
                "er nu zijn), bij onthoudingen verandert de benodigde meerderheid naar "
                "verhouding; stem wijzigen of onthouden mag tot sluiting.*"
            )

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
            stemming_line,
        ]
        await send_template_chunks(interaction, "\n".join(parts))


class StembureauPostModal1(discord.ui.Modal, title="Nieuwe stemronde (1/2)"):
    def __init__(self, *, config: dict, client) -> None:
        super().__init__()
        self._config = config
        self._client = client

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
        titel = str(self.titel).strip()
        debat_link = str(self.debat_link).strip()
        openbaarheid_line = format_openbaarheid(str(self.openbaarheid))
        onderwerp = str(self.onderwerp).strip()

        await interaction.response.send_message(
            content="✅ Eerste deel opgeslagen. Klik hieronder om de motie zelf in te vullen.",
            view=OpenFormView(
                lambda: StembureauPostModal2(
                    tag_line=tag_line,
                    titel=titel,
                    debat_link=debat_link,
                    openbaarheid_line=openbaarheid_line,
                    onderwerp=onderwerp,
                    client=self._client,
                    config=self._config,
                )
            ),
            ephemeral=True,
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

        await interaction.response.send_modal(
            StembureauPostModal1(config=self.config, client=self._client)
        )


async def setup(bot) -> None:
    await bot.add_cog(StembureauPostCog(bot))
