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
from typing import Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

_NUMBER_EMOJI: dict[int, str] = {
    0: ":zero:", 1: ":one:", 2: ":two:", 3: ":three:", 4: ":four:",
    5: ":five:", 6: ":six:", 7: ":seven:", 8: ":eight:", 9: ":nine:",
    10: ":keycap_ten:",
}

_MESSAGE_LIMIT = 1970  # Discord's hard cap is 2000; leave room for the ``` code-fence wrapper

_CONGRESS_ROLE_KEYS = ("congreslid", "government", "president", "vice_president")


def _format_openbaarheid(raw: str) -> str:
    """Turn a short answer ("0", "3", "C: ...") into the matching template line.

    Mirrors the three options from the manual template: 0 = immediately
    public, N = public after N days, C = conditional (with an optional
    reason typed after the "C"). Anything that doesn't match either shape
    is passed through as-is rather than guessed at, so nothing typed by the
    user is ever silently dropped.
    """
    s = raw.strip()
    if s.isdigit():
        n = int(s)
        if n == 0:
            return f"{_NUMBER_EMOJI[0]} Voor directe openbaarheid aan het Nederlandse volk"
        emoji = _NUMBER_EMOJI.get(n)
        prefix = emoji if emoji else f"**{n}**"
        dag = "dag" if n == 1 else "dagen"
        return f"{prefix} — {n} {dag} voordat dit openbaar gemaakt mag worden"
    if s[:1].lower() == "c":
        rest = s[1:].lstrip(": -").strip()
        suffix = f" — {rest}" if rest else ""
        return f":regional_indicator_c: Conditionele openbaarheid{suffix} (vanwege nationale veiligheid)"
    return s


def _chunk_text(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Split into <=limit-char chunks, breaking only between lines — never
    mid-line, so a long paragraph can't get cut off halfway through."""
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


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
            _format_openbaarheid(str(self.openbaarheid)),
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

        chunks = _chunk_text("\n".join(parts))

        # Sent inside a ``` code block, in its own message, separate from
        # this caption — plain (non-code-blocked) markdown looked fine in
        # the preview, but selecting and copying *rendered* text (a heading,
        # a mention chip) gives you the display text, not the "# ", "<@&…>"
        # source underneath, so a normal copy silently threw the formatting
        # away. Inside a code block nothing gets re-rendered in the first
        # place, so a plain drag-select-copy already gets the exact raw
        # source. The recipient just needs to drop the leading/trailing
        # ``` line(s) after pasting — called out explicitly below since
        # that's a new manual step this didn't need before.
        try:
            await interaction.response.send_message(
                content=(
                    "📋 Kopieer de tekst **binnen** elk ```-codeblok hieronder naar het "
                    "regering-/congreskanaal (verwijder de ``` -regels zelf) — pas 'm "
                    "gerust nog aan voordat je 'm post."
                ),
                ephemeral=True,
            )
            for chunk in chunks:
                await interaction.followup.send(content=f"```\n{chunk}\n```", ephemeral=True)
        except discord.HTTPException:
            logger.exception("motie: failed to send generated motion text")


class MotieCog(CommandCogBase, name="motie"):
    """Slash command /motie — motie-sjabloon generator voor het congres."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def _is_congress_member(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        roles_cfg = self.config.get("roles", {})
        allowed_ids = {roles_cfg.get(k) for k in _CONGRESS_ROLE_KEYS} - {None}
        member_role_ids = {r.id for r in member.roles}
        return bool(member_role_ids & allowed_ids)

    def _allowed_guild_ids(self) -> set[int]:
        """Production guild always; war guild too, for easier testing."""
        ids: set[int] = set()
        gid = self.config.get("guild_id")
        if gid:
            ids.add(int(gid))
        war_gid = (self.config.get("war_guild") or {}).get("guild_id")
        if war_gid:
            ids.add(int(war_gid))
        return ids

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
        if not interaction.guild or interaction.guild.id not in self._allowed_guild_ids():
            await interaction.response.send_message(
                "❌ Dit commando is hier niet beschikbaar.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not self._is_congress_member(member):
            await interaction.response.send_message(
                "❌ Alleen congresleden (of regering/president/vicepresident) "
                "kunnen een motie aanmaken.",
                ephemeral=True,
            )
            return

        roles_cfg = self.config.get("roles", {})

        def _mention(key: str) -> Optional[str]:
            rid = roles_cfg.get(key)
            return f"<@&{rid}>" if rid else None

        mentions = [_mention("congreslid")]
        if president:
            mentions.append(_mention("president"))
        if vicepresident:
            mentions.append(_mention("vice_president"))
        if ministers:
            mentions.append(_mention("government"))
        tag_line = " ".join(m for m in mentions if m) or "@Congreslid"

        await interaction.response.send_modal(
            MotieModal(tag_line=tag_line, indiener=member.mention)
        )


async def setup(bot) -> None:
    await bot.add_cog(MotieCog(bot))
