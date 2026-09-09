"""Verification cog for COPErator, the COPE alliance Discord bot.

Flow:
  1. Member clicks the verify button → modal asks for their WarEra profile
     URL → a private ticket channel is created (URL stored in the channel
     topic, same trick as nigeria_bot).
  2. Bot asks the member to post a screenshot of their profile in that
     channel — purely instructional, not programmatically checked (same as
     nigeria_bot: staff eyeball it before approving).
  3. Staff clicks Approve → bot fetches user.getUserById, resolves their
     country + government position, sets their nickname and country role,
     saves the Discord<->WarEra link, and schedules the ticket channel for
     deletion in TICKET_AUTOCLOSE_HOURS.

English throughout — unlike nigeria_bot/motie.py, this alliance spans many
countries (NL, Nigeria, Luxemburg, Croatia, Cuba, Kyrgyzstan, Equatorial
Guinea, ...), so English is the actual shared language here.

Commands (admin-role only — ADMIN_ROLE_ID):
  /postverify   — Post the verify button in the current channel
  /link         — Manually link a Discord member to a WarEra profile
                  (no ticket needed — for members who joined before this
                  bot existed)

Nickname format: "[<COUNTRY CODE><government emoji>] <warera username>",
e.g. "[NL🛡️] stavexe". The government emoji (at most one, most-senior-role-
wins) is only added when the profile's own country matches the government
position's country — see _government_info.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cope_bot.db import (
    add_pending_ticket_deletion,
    get_all_country_roles,
    get_all_links,
    get_pending_ticket_deletions,
    remove_pending_ticket_deletion,
    save_country_role,
    save_link,
)

logger = logging.getLogger("cope_bot.cog")

# ── Configuration ─────────────────────────────────────────────────────────────

GUILD_ID = 1545029330533486672
ADMIN_ROLE_ID = 1545097686464598107

TICKET_CATEGORY_NAME = "🔐 Verification"
TICKET_AUTOCLOSE_HOURS = 1

WARERA_API_BASE = "https://api2.warera.io/trpc"
WARERA_API_KEY = os.environ.get("WARERA_API_KEY", "")

# Delay between consecutive API calls during the periodic sync sweep.
# With an API key: 0.15s ≈ 400 req/min. Without: 0.7s ≈ 85 req/min (under
# the 100/min anonymous limit) — same numbers as nigeria_bot's nick_sync.
_SYNC_DELAY = 0.15 if WARERA_API_KEY else 0.7

_WARERA_URL_RE = re.compile(
    r"https?://(?:app\.)?warera\.io/user/([0-9a-f]{24})", re.IGNORECASE
)

# (profile field under "infos", emoji, human label) — checked in this order,
# so the most senior position wins if someone somehow holds more than one.
_GOVERNMENT_ROLES: list[tuple[str, str, str]] = [
    ("presidentOf", "★", "President"),
    ("vicePresidentOf", "☆", "Vice President"),
    ("minOfDefenseOf", "🛡️", "Minister of Defense"),
    ("minOfEconomyOf", "📈", "Minister of Economy"),
    ("minOfForeignAffairsOf", "🌐", "Minister of Foreign Affairs"),
]

# Known country code (lowercase, as returned by country.getCountryById's
# "code" field) -> Discord role id. A country not listed here gets a role
# auto-created (and cached in the country_roles DB table) the first time a
# member from it verifies — see _get_or_create_country_role.
COUNTRY_ROLE_IDS: dict[str, int] = {
    "nl": 1545782897682939944,   # The Netherlands
    "ng": 1545783056877486090,   # Nigeria
    "lu": 1545783165799628830,   # Luxemburg
    "hr": 1545783706495488000,   # Croatia
    "cu": 1545789680648196246,   # Cuba
    "kg": 1545790970572832869,   # Kyrgyzstan
    "gq": 1547213678028263434,   # Equatorial Guinea
}

# country_id -> role_id, seeded from the DB at cog load and grown at runtime
# as new countries are auto-created. Module-level and process-global on
# purpose — same pattern as nigeria_bot's AMBASSADOR_ROLES dict.
_country_role_cache: dict[str, int] = {}

# ── WarEra API ────────────────────────────────────────────────────────────────

def _unwrap(resp: object) -> Optional[dict]:
    """Strip a single tRPC result/data envelope."""
    if isinstance(resp, dict):
        inner = resp.get("result", resp)
        if isinstance(inner, dict):
            inner = inner.get("data", inner)
        return inner if isinstance(inner, dict) else None
    return None


async def _warera_get(
    procedure: str, input_obj: dict, session: Optional[aiohttp.ClientSession] = None
) -> Optional[dict]:
    """GET one tRPC procedure, retrying once on HTTP 429."""
    url = f"{WARERA_API_BASE}/{procedure}"
    params = {"input": json.dumps(input_obj)}
    headers = {"x-api-key": WARERA_API_KEY} if WARERA_API_KEY else {}

    async def _do_fetch(sess: aiohttp.ClientSession) -> Optional[dict]:
        for attempt in range(2):
            try:
                async with sess.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        if attempt == 0:
                            await asyncio.sleep(2)
                            continue
                        logger.warning("_warera_get(%s): still 429 after retry", procedure)
                        return None
                    if resp.status != 200:
                        logger.warning("_warera_get(%s): HTTP %d", procedure, resp.status)
                        return None
                    return _unwrap(await resp.json())
            except Exception as exc:
                logger.warning("_warera_get(%s): exception: %s", procedure, exc)
                return None
        return None

    if session is not None:
        return await _do_fetch(session)
    async with aiohttp.ClientSession() as sess:
        return await _do_fetch(sess)


async def _fetch_warera_user(user_id: str, session: Optional[aiohttp.ClientSession] = None) -> Optional[dict]:
    return await _warera_get("user.getUserById", {"userId": user_id}, session)


async def _fetch_warera_country(country_id: str, session: Optional[aiohttp.ClientSession] = None) -> Optional[dict]:
    return await _warera_get("country.getCountryById", {"countryId": country_id}, session)


def _extract_warera_id(url: str) -> Optional[str]:
    m = _WARERA_URL_RE.search(url)
    return m.group(1) if m else None


def _government_info(user_doc: dict, own_country_id: Optional[str]) -> tuple[Optional[str], str, str]:
    """Return (profile field, emoji, label) for the member's government
    position, or (None, "", "") if they hold none.

    Only counts a position if it's for the member's OWN current country —
    guards against a stale field surviving a citizenship change.
    """
    infos = user_doc.get("infos") or {}
    for field, emoji, label in _GOVERNMENT_ROLES:
        value = infos.get(field)
        if not value:
            continue
        if own_country_id and str(value) != str(own_country_id):
            continue
        return field, emoji, label
    return None, "", ""


def _build_nickname(code: str, gov_emoji: str, username: str) -> str:
    """"[<CODE><emoji>] <username>", truncated to Discord's 32-char nickname cap."""
    tag = f"{code}{gov_emoji}"
    prefix = f"[{tag}] " if tag else ""
    max_name_len = 32 - len(prefix)
    if max_name_len <= 0:
        return prefix.strip()[:32]
    return prefix + username[:max_name_len]

# ── Country roles ─────────────────────────────────────────────────────────────

async def _preload_country_role_cache(db: aiosqlite.Connection) -> None:
    for country_id, role_id, _code, _name in await get_all_country_roles(db):
        _country_role_cache[country_id] = int(role_id)


async def _get_or_create_country_role(
    guild: discord.Guild, db: aiosqlite.Connection, country_id: str, code: str, name: str,
) -> Optional[discord.Role]:
    role_id = COUNTRY_ROLE_IDS.get(code.lower()) if code else None
    if not role_id:
        role_id = _country_role_cache.get(country_id)
    if role_id:
        role = guild.get_role(int(role_id))
        if role:
            return role

    role_name = (name or code or country_id)[:100]
    for role in guild.roles:
        if role.name.lower() == role_name.lower():
            await save_country_role(db, country_id, str(role.id), code, name)
            _country_role_cache[country_id] = role.id
            return role

    try:
        new_role = await guild.create_role(
            name=role_name, mentionable=True,
            reason=f"Nationality role automatically created for {role_name}",
        )
    except discord.Forbidden:
        logger.warning("_get_or_create_country_role: no permission to create role for %s", role_name)
        return None
    await save_country_role(db, country_id, str(new_role.id), code, name)
    _country_role_cache[country_id] = new_role.id
    logger.info("Created country role %r (id=%d) for country_id=%s", role_name, new_role.id, country_id)
    return new_role

# ── Shared verification logic (used by ticket-approve, /link, and the sync loop) ─

async def _apply_verification(
    guild: discord.Guild,
    member: discord.Member,
    warera_id: str,
    db: aiosqlite.Connection,
    *,
    approver: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> tuple[str, list[str]]:
    """Fetch the WarEra profile, set nickname + country role, persist the
    link. Returns (short result summary, warnings)."""
    user_doc = await _fetch_warera_user(warera_id, session=session)
    if not user_doc:
        return "❌ Could not fetch WarEra profile — is the URL correct?", []

    username = user_doc.get("username") or member.name
    country_id = user_doc.get("country")
    code = ""
    country_name = ""
    if country_id:
        country_doc = await _fetch_warera_country(country_id, session=session)
        if country_doc:
            code = str(country_doc.get("code") or "").upper()
            country_name = country_doc.get("name") or country_id

    gov_field, gov_emoji, gov_label = _government_info(user_doc, country_id)

    warnings: list[str] = []

    if country_id:
        role = await _get_or_create_country_role(guild, db, country_id, code, country_name)
        if role:
            tracked_ids = set(COUNTRY_ROLE_IDS.values()) | set(_country_role_cache.values())
            current_ids = {r.id for r in member.roles}
            to_remove = [r for r in member.roles if r.id in tracked_ids and r.id != role.id]
            try:
                if to_remove:
                    await member.remove_roles(*to_remove, reason=f"Country role update by {approver}")
                if role.id not in current_ids:
                    await member.add_roles(role, reason=f"Country role set by {approver}")
            except discord.Forbidden:
                warnings.append(f"No permission to update country role **{role.name}**.")
        else:
            warnings.append("Could not find or create a country role for this member.")
    else:
        warnings.append("No country found on the WarEra profile.")

    nickname = _build_nickname(code, gov_emoji, username)
    current_nick = member.nick or member.name
    if current_nick != nickname:
        try:
            await member.edit(nick=nickname, reason=f"WarEra verification by {approver}")
        except discord.Forbidden:
            warnings.append("No permission to set nickname (role too high?).")

    await save_link(
        db, str(member.id), warera_id,
        username=username, country_id=country_id, government_role=gov_field,
    )

    summary = f"✅ {member.mention} → **{nickname}**"
    if gov_label:
        summary += f" ({gov_label})"
    return summary, warnings

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.id == ADMIN_ROLE_ID for r in member.roles)


def _admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and _is_admin(interaction.user):
            return True
        raise app_commands.MissingPermissions(["admin_role"])
    return app_commands.check(predicate)


def _parse_topic(topic: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in (topic or "").split("|"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result


def _ticket_already_closed(channel: discord.TextChannel) -> bool:
    return channel.name.startswith("closed-")


async def _get_or_create_ticket_category(guild: discord.Guild) -> discord.CategoryChannel:
    for cat in guild.categories:
        if cat.name == TICKET_CATEGORY_NAME:
            return cat
    return await guild.create_category(
        TICKET_CATEGORY_NAME, reason="Verification ticket category automatically created"
    )


async def _close_ticket_later(
    channel: discord.TextChannel,
    hours: int = TICKET_AUTOCLOSE_HOURS,
    db: Optional[aiosqlite.Connection] = None,
    *,
    delete_at: Optional[datetime] = None,
) -> None:
    """Rename the channel to signal it's closed, then delete it after *hours*.

    delete_at is persisted to *db* so _reconcile_pending_deletions (called
    from on_ready) can resume the wait across a restart — same reasoning as
    nigeria_bot's identical helper.
    """
    if delete_at is None:
        delete_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        try:
            new_name = f"closed-{channel.name}"[:100]
            await channel.edit(name=new_name, reason=f"Ticket closed, deleted in {hours}h")
        except Exception as exc:
            logger.warning("_close_ticket_later: could not rename channel %s: %s", channel.id, exc)
        if db is not None:
            try:
                await add_pending_ticket_deletion(db, str(channel.id), delete_at.isoformat())
            except Exception:
                logger.warning("_close_ticket_later: could not persist pending deletion for %s", channel.id)

    remaining = (delete_at - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining)
    try:
        await channel.delete(reason=f"Ticket automatically deleted after {hours}h")
    except Exception as exc:
        logger.warning("_close_ticket_later: could not delete channel %s: %s", channel.id, exc)
        return
    if db is not None:
        try:
            await remove_pending_ticket_deletion(db, str(channel.id))
        except Exception:
            logger.warning("_close_ticket_later: could not clear pending deletion for %s", channel.id)


async def _reconcile_pending_deletions(bot: commands.Bot, db: aiosqlite.Connection) -> None:
    try:
        records = await get_pending_ticket_deletions(db)
    except Exception:
        logger.exception("_reconcile_pending_deletions: failed to read pending deletions")
        return
    if not records:
        return
    logger.info("_reconcile_pending_deletions: resuming %d pending ticket deletion(s)", len(records))
    for channel_id, delete_at_str in records:
        try:
            delete_at = datetime.fromisoformat(delete_at_str)
        except Exception:
            delete_at = datetime.now(timezone.utc)
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            try:
                await remove_pending_ticket_deletion(db, channel_id)
            except Exception:
                pass
            continue
        asyncio.create_task(_close_ticket_later(channel, db=db, delete_at=delete_at))


async def _create_ticket(interaction: discord.Interaction, warera_url: str) -> None:
    guild = interaction.guild
    user = interaction.user

    safe_user = str(user.id)[-6:]
    channel_name = f"verify-{safe_user}"
    category = await _get_or_create_ticket_category(guild)

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        )

    # Store the URL in the topic so Approve can read it without staff retyping it.
    topic = f"user_id={user.id}|warera_url={warera_url}"
    channel = await category.create_text_channel(
        name=channel_name, topic=topic, overwrites=overwrites,
        reason=f"Verification ticket for {user}",
    )

    info_embed = discord.Embed(
        title="🎫 Verification request",
        description=f"**Discord user:** {user.mention}\n**WarEra profile:** {warera_url}",
        colour=discord.Colour.orange(),
    )
    await channel.send(embed=info_embed)
    await channel.send(
        f"{user.mention} Thank you! Please send a **screenshot of your WarEra profile** "
        "in this channel to complete your verification.\n\n"
        "📸 **How to find your profile:**\n"
        "Go to WarEra → click your profile picture in the top right → click **Profile**."
    )
    staff_embed = discord.Embed(
        title="📋 Staff instructions",
        description=(
            f"Check the profile and screenshot of {user.mention}.\n\n"
            "Use the buttons below to approve or deny this request."
        ),
        colour=discord.Colour.blurple(),
    )
    await channel.send(embed=staff_embed, view=TicketActionView())

    await interaction.followup.send(
        f"✅ Your verification request has been created in {channel.mention}.\n"
        "Please send a screenshot of your WarEra profile there to complete verification.",
        ephemeral=True,
    )

# ── Modal + retry ─────────────────────────────────────────────────────────────

class _RetryView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=120)

    @discord.ui.button(label="↩️ Try again", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(VerifyModal())


class VerifyModal(discord.ui.Modal, title="COPE Verification"):
    warera_url = discord.ui.TextInput(
        label="WarEra profile URL",
        placeholder="https://app.warera.io/user/69768dd97ad3b1bff3c53882",
        min_length=10,
        max_length=120,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = self.warera_url.value.strip()
        if not _extract_warera_id(url):
            await interaction.response.send_message(
                "❌ **Invalid URL.** Make sure your URL looks exactly like this:\n"
                "`https://app.warera.io/user/69768dd97ad3b1bff3c53882`\n\n"
                "Click the button to try again.",
                ephemeral=True,
                view=_RetryView(),
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _create_ticket(interaction, url)

# ── Ticket action buttons ─────────────────────────────────────────────────────

class DenyReasonModal(discord.ui.Modal, title="Reject verification"):
    reason_input = discord.ui.TextInput(
        label="Reason (optional)",
        placeholder="E.g. profile not found, screenshot doesn't match…",
        required=False,
        max_length=200,
    )

    def __init__(self, button_message: discord.Message) -> None:
        super().__init__()
        self.button_message = button_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await self.button_message.edit(view=None)
        except Exception:
            pass
        reason = self.reason_input.value.strip()
        await interaction.channel.send(
            f"❌ **Denied** by {interaction.user.mention}"
            + (f"\nReason: {reason}" if reason else "")
            + f"\nThis channel will be deleted in {TICKET_AUTOCLOSE_HOURS} hour(s)."
        )
        await interaction.followup.send("✅ Ticket denied.", ephemeral=True)
        db = getattr(interaction.client, "cope_db", None)
        asyncio.create_task(_close_ticket_later(interaction.channel, db=db))


class TicketActionView(discord.ui.View):
    """Persistent approve/deny buttons posted inside every ticket channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="cope_ticket:approve")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Only staff can do this.", ephemeral=True)
            return
        if _ticket_already_closed(interaction.channel):
            await interaction.response.send_message("⚠️ This ticket has already been processed.", ephemeral=True)
            return

        ctx = _parse_topic(interaction.channel.topic or "")
        user_id = ctx.get("user_id", "")
        warera_url = ctx.get("warera_url", "")
        warera_id = _extract_warera_id(warera_url)

        await interaction.response.defer(ephemeral=True)

        try:
            member = await interaction.guild.fetch_member(int(user_id))
        except Exception:
            await interaction.followup.send("❌ User is no longer in the server.", ephemeral=True)
            return
        if not warera_id:
            await interaction.followup.send("❌ No valid WarEra URL found in the ticket.", ephemeral=True)
            return

        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        await interaction.followup.send("⏳ Fetching profile from WarEra…", ephemeral=True)
        db = getattr(interaction.client, "cope_db", None)
        summary, warnings = await _apply_verification(
            interaction.guild, member, warera_id, db, approver=str(interaction.user),
        )

        await interaction.channel.send(
            f"✅ **Approved** by {interaction.user.mention}\n"
            f"This channel will be deleted in {TICKET_AUTOCLOSE_HOURS} hour(s)."
        )
        result_msg = summary
        if warnings:
            result_msg += "\n⚠️ " + " | ".join(warnings)
        await interaction.followup.send(result_msg, ephemeral=True)
        asyncio.create_task(_close_ticket_later(interaction.channel, db=db))

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, custom_id="cope_ticket:deny")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Only staff can do this.", ephemeral=True)
            return
        if _ticket_already_closed(interaction.channel):
            await interaction.response.send_message("⚠️ This ticket has already been processed.", ephemeral=True)
            return
        await interaction.response.send_modal(DenyReasonModal(button_message=interaction.message))

# ── Persistent "start verification" view ──────────────────────────────────────

class VerificationView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Verify", style=discord.ButtonStyle.success, custom_id="cope_verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(VerifyModal())

# ── Cog ──────────────────────────────────────────────────────────────────────

class VerificationCog(commands.Cog, name="cope_verification"):
    def __init__(self, bot: commands.Bot, db: aiosqlite.Connection) -> None:
        self.bot = bot
        self._db = db

    async def cog_load(self) -> None:
        await _preload_country_role_cache(self._db)
        self.nickname_role_sync.start()

    def cog_unload(self) -> None:
        self.nickname_role_sync.cancel()

    # ── Periodic nickname/role sync ───────────────────────────────────────────

    @tasks.loop(hours=6)
    async def nickname_role_sync(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        links = await get_all_links(self._db)
        if not links:
            return

        processed = 0
        async with aiohttp.ClientSession() as sess:
            for i, (discord_id, warera_id) in enumerate(links):
                if i > 0:
                    await asyncio.sleep(_SYNC_DELAY)
                member = guild.get_member(int(discord_id))
                if not member:
                    continue
                try:
                    await _apply_verification(
                        guild, member, warera_id, self._db,
                        approver="automatic sync", session=sess,
                    )
                    processed += 1
                except Exception:
                    logger.exception("nickname_role_sync: failed for %s", discord_id)
        logger.info("nickname_role_sync: processed %d/%d linked member(s)", processed, len(links))

    @nickname_role_sync.before_loop
    async def _before_sync(self) -> None:
        await self.bot.wait_until_ready()

    # ── /postverify ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="postverify",
        description="Post the verification button in this channel.",
    )
    @_admin_check()
    async def post_verify(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="🪖 COPE Verification",
            description=(
                "Click the button below to verify your WarEra account.\n\n"
                "You'll be asked for your WarEra profile URL "
                "(`https://app.warera.io/user/...`). A private channel will "
                "then be created for you — send a screenshot of your profile "
                "there so staff can confirm it's you."
            ),
            colour=discord.Colour(0x2ECC71),
        )
        await interaction.channel.send(embed=embed, view=VerificationView())
        await interaction.followup.send("✅ Verification button posted.", ephemeral=True)

    # ── /link (manual admin linking) ──────────────────────────────────────────

    @app_commands.command(
        name="link",
        description="Manually link a Discord member to their WarEra profile.",
    )
    @app_commands.describe(
        user="The Discord member to link.",
        warera_url="Their WarEra profile URL, e.g. https://app.warera.io/user/69768dd97ad3b1bff3c53882",
    )
    @_admin_check()
    async def link(
        self, interaction: discord.Interaction, user: discord.Member, warera_url: str
    ) -> None:
        warera_id = _extract_warera_id(warera_url.strip())
        if not warera_id:
            await interaction.response.send_message(
                "❌ Invalid URL. It must look exactly like:\n"
                "`https://app.warera.io/user/69768dd97ad3b1bff3c53882`",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        summary, warnings = await _apply_verification(
            interaction.guild, user, warera_id, self._db, approver=str(interaction.user),
        )
        msg = summary
        if warnings:
            msg += "\n⚠️ " + " | ".join(warnings)
        await interaction.followup.send(msg, ephemeral=True)
