"""Slash command /exportberichten — exporteer alle berichten in het huidige
kanaal naar een tekstbestand, met per bericht de afzender en het tijdstip.
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import is_owner_or_admin

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# Stay well under Discord's lowest per-file upload cap (8 MB for
# non-boosted servers) so large channels still export cleanly, split
# across multiple files.
_MAX_FILE_BYTES = 7 * 1024 * 1024
# Discord allows at most 10 attachments per message.
_MAX_FILES_PER_MESSAGE = 10
# How often (seconds) to push a "still working" progress edit while paging
# through history — large channels can take a while, and a slash command
# with no feedback for a minute+ looks hung.
_PROGRESS_INTERVAL_S = 4.0


def _safe_filename(name: str) -> str:
    """Strip characters that are awkward in a downloaded filename."""
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or "kanaal"


class ExportBerichtenCog(commands.Cog, name="exportberichten"):
    """Exporteert de volledige berichtgeschiedenis van een kanaal."""

    def __init__(self, bot: "DiscordBot") -> None:
        self.bot = bot

    @app_commands.command(
        name="exportberichten",
        description="Exporteer alle berichten in dit kanaal naar een tekstbestand (met afzender + tijdstip).",
    )
    @is_owner_or_admin()
    async def exportberichten(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Dit commando werkt alleen in tekst-kanalen en threads.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.edit_original_response(content="⏳ Berichten ophalen...")

        # Full history, oldest first, so the exported file reads top-to-bottom
        # in the order messages were actually sent.
        lines: list[str] = []
        last_progress_at = time.monotonic()
        try:
            async for msg in channel.history(limit=None, oldest_first=True):
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                content = msg.content or ""
                if msg.attachments:
                    names = ", ".join(a.filename for a in msg.attachments)
                    content = f"{content} [bijlage: {names}]" if content else f"[bijlage: {names}]"
                if not content:
                    content = "[leeg bericht — alleen embed/sticker]"
                lines.append(f"[{ts}] {msg.author.display_name}: {content}")

                now = time.monotonic()
                if now - last_progress_at >= _PROGRESS_INTERVAL_S:
                    last_progress_at = now
                    try:
                        await interaction.edit_original_response(
                            content=f"⏳ Berichten ophalen... **{len(lines)}** tot nu toe."
                        )
                    except discord.HTTPException:
                        pass
        except discord.Forbidden:
            await interaction.followup.send(
                "De bot heeft geen toestemming om berichten in dit kanaal te lezen.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.exception("exportberichten: failed to fetch channel history")
            await interaction.followup.send(
                f"❌ Fout bij ophalen van berichten: {exc}", ephemeral=True
            )
            return

        if not lines:
            await interaction.followup.send(
                "Geen berichten gevonden in dit kanaal.", ephemeral=True
            )
            return

        try:
            await interaction.edit_original_response(
                content=f"⏳ {len(lines)} berichten opgehaald — bestand bouwen..."
            )
        except discord.HTTPException:
            pass

        channel_label = _safe_filename(getattr(channel, "name", str(channel.id)))

        # Split into multiple files if the export exceeds Discord's upload cap.
        encoded_lines = [(line + "\n").encode("utf-8") for line in lines]
        file_chunks: list[bytes] = []
        current = bytearray()
        for line_bytes in encoded_lines:
            if current and len(current) + len(line_bytes) > _MAX_FILE_BYTES:
                file_chunks.append(bytes(current))
                current = bytearray()
            current += line_bytes
        if current:
            file_chunks.append(bytes(current))

        files = [
            discord.File(
                io.BytesIO(chunk),
                filename=(
                    f"{channel_label}_export.txt"
                    if len(file_chunks) == 1
                    else f"{channel_label}_export_{i + 1}of{len(file_chunks)}.txt"
                ),
            )
            for i, chunk in enumerate(file_chunks)
        ]

        summary = f"✅ **{len(lines)}** berichten geëxporteerd uit **#{channel_label}**."
        for i in range(0, len(files), _MAX_FILES_PER_MESSAGE):
            batch = files[i : i + _MAX_FILES_PER_MESSAGE]
            if i == 0:
                # Edit the original "thinking" message in place rather than
                # sending a separate followup, so there's no stray
                # "bestand bouwen..." message left behind once this is done.
                await interaction.edit_original_response(content=summary, attachments=batch)
            else:
                await interaction.followup.send(files=batch, ephemeral=True)


async def setup(bot: "DiscordBot") -> None:
    """Add the ExportBerichtenCog to the bot."""
    await bot.add_cog(ExportBerichtenCog(bot))
