"""Shared helpers for the congress motion/debate-template family of slash
commands (/motie, /debat, /stembureaupost, /stembureauarchiefpost,
/motieafgerond).

None of these commands post anything on the user's behalf — they all just
assemble a block of markdown text (a filled-in template) and hand it back
to the user, ephemeral and code-fenced, for them to copy into the actual
congress channel themselves. What's factored out here is everything that
repeats across that family:

- Guild gating (production guild always; the war guild too, since the main
  bot process serves both and the war guild is a much easier place to test
  against).
- Congress-role gating (congreslid/government/president/vice_president, or
  admin) — the same "congress-adjacent" set already used for
  samenvatting_allowed elsewhere in this codebase.
- Openbaarheidsstatus short-code formatting ("0" / "3" / "C: reden" -> the
  matching template line), used by both /motie and /stembureaupost.
- Chunking + sending the generated text back to the user, code-fenced so a
  plain select-and-copy on ANY client (including mobile, which has no
  hover-copy icon and instead needs Discord's own long-press "Tekst
  kopiëren") grabs the raw markdown/mentions rather than the rendered
  display text.
- A generic "show a description, then open the modal on button-press" flow
  for commands that want explanatory text ahead of their form. Discord
  modals cap out at 5 top-level components total (discord.py enforces this
  even for the newer TextDisplay component), so a modal already using
  several TextInputs often has no room left for a description block inside
  it — showing the description as a preceding message instead sidesteps
  that limit entirely and is guaranteed to render the same on every client
  (TextDisplay-in-modal is a very recent Components V2 addition without
  guaranteed mobile parity yet). It's ALSO how multi-modal commands
  (/motie, /stembureaupost) chain their two modals — a modal opened
  directly from another modal's on_submit hits a live Discord API 400
  ("In type: Value must be one of {4, 5, 6, 7, 10, 12}"): discord.py 2.6.4
  still wraps modal fields in the now-deprecated ActionRow structure, which
  Discord accepts for a modal opened from a slash command/button/select but
  rejects for one opened from a modal submission. Button->modal is a
  different, unaffected code path, so that's the button OpenFormView shows
  in both cases — description text and modal-chaining are the same problem
  from Discord's component system's point of view.
- Live congress-size lookup for /stembureaupost's majority-threshold
  sentence (fetch_congress_majority) — the Dutch congress's member count
  changes with monthly elections, so a hardcoded "18 votes needed out of
  35" goes stale; this pulls the live count from government.getByCountryId
  instead.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

import discord

logger = logging.getLogger("discord_bot")

CONGRESS_ROLE_KEYS = ("congreslid", "government", "president", "vice_president")

MESSAGE_LIMIT = 1970  # Discord's hard cap is 2000; leave room for the ``` code-fence wrapper

NUMBER_EMOJI: dict[int, str] = {
    0: ":zero:", 1: ":one:", 2: ":two:", 3: ":three:", 4: ":four:",
    5: ":five:", 6: ":six:", 7: ":seven:", 8: ":eight:", 9: ":nine:",
    10: ":keycap_ten:",
}


def is_congress_member(config: dict, member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    roles_cfg = config.get("roles", {})
    allowed_ids = {roles_cfg.get(k) for k in CONGRESS_ROLE_KEYS} - {None}
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & allowed_ids)


def allowed_guild_ids(config: dict) -> set[int]:
    """Production guild always; war guild too, for easier testing."""
    ids: set[int] = set()
    gid = config.get("guild_id")
    if gid:
        ids.add(int(gid))
    war_gid = (config.get("war_guild") or {}).get("guild_id")
    if war_gid:
        ids.add(int(war_gid))
    return ids


def role_mention(config: dict, key: str) -> Optional[str]:
    rid = (config.get("roles") or {}).get(key)
    return f"<@&{rid}>" if rid else None


def format_openbaarheid(raw: str) -> str:
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
            return f"{NUMBER_EMOJI[0]} Voor directe openbaarheid aan het Nederlandse volk"
        emoji = NUMBER_EMOJI.get(n)
        prefix = emoji if emoji else f"**{n}**"
        dag = "dag" if n == 1 else "dagen"
        return f"{prefix} — {n} {dag} voordat dit openbaar gemaakt mag worden"
    if s[:1].lower() == "c":
        rest = s[1:].lstrip(": -").strip()
        suffix = f" — {rest}" if rest else ""
        return f":regional_indicator_c: Conditionele openbaarheid{suffix} (vanwege nationale veiligheid)"
    return s


def _unwrap_trpc(resp: object) -> object:
    """Unwrap {"result": {"data": ...}} tRPC response envelope."""
    try:
        return resp["result"]["data"]  # type: ignore[index]
    except (KeyError, TypeError):
        return resp


def format_half(n: int) -> str:
    half = n / 2
    if half == int(half):
        return str(int(half))
    return f"{half:.1f}".replace(".", ",")


async def fetch_congress_majority(client, config: dict) -> Optional[tuple[int, int]]:
    """Live (total eligible voters, majority needed) for the Dutch congress.

    The member count changes with monthly congress elections, so this can't
    be a hardcoded constant — see the module docstring for the incident
    that prompted pulling it live from government.getByCountryId instead.
    Total is len(congressMembers) + 2 — President and Vice President can
    also vote and aren't necessarily congress members themselves, unlike
    the ministers (who, per the NicolasKing example this session, are also
    tagged as a regular congressMemberOf and so are already counted in that
    list). Majority is a simple >50% threshold (n // 2 + 1); returns None
    on any failure (no client, no nl_country_id configured, API error, or
    an unexpected response shape) so callers can fall back to a
    number-free sentence rather than silently showing a wrong number.
    """
    if not client:
        return None
    nl_country_id = config.get("nl_country_id")
    if not nl_country_id:
        return None
    try:
        raw = await client.get(
            "/government.getByCountryId",
            params={"input": json.dumps({"countryId": nl_country_id})},
        )
    except Exception:
        logger.exception("congress_templates: government.getByCountryId failed")
        return None
    data = _unwrap_trpc(raw)
    if not isinstance(data, dict):
        return None
    members = data.get("congressMembers")
    if not isinstance(members, list) or not members:
        return None
    total = len(members) + 2  # + President + Vice President
    return total, total // 2 + 1


def build_tag_line(
    config: dict, president: bool, vicepresident: bool, ministers: bool, *, base: str = "congreslid"
) -> str:
    """congreslid (or *base*) always, plus whichever of president/vice
    president/ministers were requested.

    Command-option booleans, not a modal Select — a Select field inside a
    modal was tried first (the closest thing to checkboxes Discord's modal
    components offer) but is rejected by Discord's API in practice
    (confirmed live: the same "In type: Value must be one of {4, 5, 6, 7,
    10, 12}" 400 as the modal-chaining issue, this time on a modal opened
    from a plain button click with no chaining involved — so the Select
    field itself is the problem, not how the modal was opened). Falling
    back to command-option booleans, same as before that attempt.
    """
    mentions = [role_mention(config, base)]
    if president:
        mentions.append(role_mention(config, "president"))
    if vicepresident:
        mentions.append(role_mention(config, "vice_president"))
    if ministers:
        mentions.append(role_mention(config, "government"))
    return " ".join(m for m in mentions if m) or f"@{base.capitalize()}"


def normalize_uitslag(raw: str) -> str:
    """"aangenomen" / "geweigerd" from a free-text answer, or the raw text
    unchanged if it doesn't clearly match either — same forgiving philosophy
    as format_openbaarheid: never silently drop what the user typed."""
    s = raw.strip().lower()
    if s in ("aangenomen", "aan", "ja", "accepted", "passed", "goedgekeurd"):
        return "aangenomen"
    if s in ("geweigerd", "afgewezen", "nee", "rejected", "denied"):
        return "geweigerd"
    return raw.strip()


def chunk_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
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


async def send_template_chunks(interaction: discord.Interaction, body: str) -> None:
    """Send *body* back to the user as one or more ephemeral, code-fenced
    messages, as the first response to *interaction*.

    Sent inside ``` code blocks, in their own messages, separate from the
    caption — plain (non-code-blocked) markdown looks fine in the preview,
    but selecting and copying *rendered* text (a heading, a mention chip)
    gives you the display text, not the "# ", "<@&…>" source underneath, so
    a normal copy would silently throw the formatting away. Inside a code
    block nothing gets re-rendered in the first place, so it copies clean.

    Desktop and mobile differ in what exactly ends up on the clipboard,
    which is why the caption below only tells MOBILE users to strip the
    ``` lines: desktop's dedicated hover copy-icon copies only the code
    block's inner content (no backticks in the result — confirmed nothing
    to remove there), while mobile has no such icon and instead uses
    Discord's generic long-press -> "Tekst kopiëren" ("Copy Text"), which
    copies the ENTIRE message including the ``` fence lines above and
    below. Telling desktop users to "remove the ``` lines" was confusing
    (the earlier caption said this unconditionally) since they'd never see
    any to remove in the first place.
    """
    caption = (
        "📋 Kopieer de tekst **binnen** elk ```-codeblok hieronder naar het "
        "regering-/congreskanaal — pas 'm gerust nog aan voordat je 'm post.\n"
        "• **Desktop:** hover over het blok en klik het kopieer-icoontje rechtsboven "
        "— dat kopieert alleen de tekst zelf.\n"
        "• **Mobiel:** er is geen kopieerknopje zichtbaar — hou het bericht ingedrukt "
        "en kies **Tekst kopiëren**. Dat kopieert het hele bericht inclusief de "
        "``` -regels boven- en onderaan — verwijder die twee regels na het plakken."
    )
    chunks = chunk_text(body)
    try:
        await interaction.response.send_message(content=caption, ephemeral=True)
        for chunk in chunks:
            await interaction.followup.send(content=f"```\n{chunk}\n```", ephemeral=True)
    except discord.HTTPException:
        logger.exception("congress_templates: failed to send generated template text")


class OpenFormView(discord.ui.View):
    """A single "open the form" button — used to show explanatory text
    ahead of a modal, since Discord modals can't reliably fit both a
    description and several input fields at once (see module docstring)."""

    def __init__(
        self,
        build_modal: Callable[[], discord.ui.Modal],
        *,
        timeout: Optional[float] = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self._build_modal = build_modal

    @discord.ui.button(label="📝 Formulier openen", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(self._build_modal())
