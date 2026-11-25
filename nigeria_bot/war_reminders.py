"""Nigeria-bot feature: oil-saving bunker reminder + war-priority reminders.

Three independent reminders, all live-API-only (see ``nigeria_bot/oliegebruik.py``
for why: upgrade status and war priority are exactly the kind of thing an
hourly-swept cache would misrepresent):

1. **Bunker-off reminder.** Nigeria's active defensive battles are polled via
   ``battle.getActiveBattlesByCountry``. When a tracked defensive battle
   disappears from that list (it ended), the battle's defender region is
   queued for a bunker check one hour later. If the bunker in that region is
   still ``active`` at that point, a reminder to turn it off (it has no
   further use once the battle is over, and costs oil every hour it stays on)
   is posted to ``_BUNKER_CHANNEL_ID``.

2. **Military-base warning.** Nigeria's wars are tracked by ``war`` id
   (discovered once via any of Nigeria's battles — active or not — and then
   polled by ``war.getById`` every cycle *regardless* of current battle
   activity, since the ``priority`` countdown mostly runs during the lull
   *between* battles, when ``battle.getActiveBattlesByCountry``/
   ``battle.getBattles(isActive=True)`` show nothing at all). A war is
   retired from tracking the moment ``war.getById`` itself reports
   ``isActive: false`` — that flag is authoritative, no guessing needed.
   When a war's ``priorityEndAt`` is 12 hours or less away, a reminder to
   turn on a military base (``base`` upgrade — 12h to activate, see
   ``gameConfig.getGameConfig().upgradesConfig.base``) in a Nigeria region
   bordering the enemy country is posted, so the base is ready exactly when
   priority runs out and both sides are free to attack again.

3. **Priority-ending-soon notice.** Same tracked wars, fired at the 2-hour
   mark — a plain heads-up, independent of the base reminder (which should
   already have gone out 10 hours earlier).

Both war-priority thresholds use ``hours_left <= N and not notified_N``, so a
cycle missed during downtime still fires late on the next poll rather than
being silently skipped, and each threshold is a one-shot per priority window
— ``notified_12h``/``notified_2h`` are reset back to 0 whenever
``priorityEndAt`` changes (a new window, e.g. after a battle in that war).

Discovering which wars Nigeria is in has no dedicated endpoint — the obvious
candidate, ``war.getActiveWarBetweenCountries``, answers 403 "API tokens
cannot access this endpoint" (see docs/api-changes-2026-11-23.md) — so
``battle.getBattles`` filtered by ``countryId`` (no ``isActive`` filter, so it
also surfaces wars whose last battle has already ended) is the only way in.
Finding a Nigeria region that borders the enemy country reuses
``region.getRegionsObject`` exactly as ``oliegebruik.py`` does, matching each
Nigeria region's ``neighbors`` list against the enemy's region ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
import discord
from discord.ext import commands, tasks

logger = logging.getLogger("nigeria_bot.war_reminders")

WARERA_API_BASE = "https://api2.warera.io/trpc"
WARERA_API_KEY = os.environ.get("WARERA_API_KEY", "")

NIGERIA_COUNTRY_ID = "683ddd2c24b5a2e114af15fa"

_BUNKER_CHANNEL_ID = 1575086155018535023
_WAR_PRIORITY_CHANNEL_ID = 1575086158931824694

_BUNKER_CHECK_DELAY = timedelta(hours=1)
# A base takes 12h to activate — warn 2h earlier than that so there's actually
# time to act before the window would otherwise close exactly as it finishes
# building. The "notified_12h" column/flag name below predates this bump; it
# still means "the base-reminder threshold", not literally 12 hours.
_BASE_WARNING_HOURS = 14
_PRIORITY_END_WARNING_HOURS = 2

_POLL_MINUTES = 5


def _unwrap(resp: object) -> dict | list | None:
    """Strip a single tRPC result/data envelope."""
    if isinstance(resp, dict):
        inner = resp.get("result", resp)
        if isinstance(inner, dict):
            return inner.get("data", inner)
        return inner
    return resp  # type: ignore[return-value]


async def _get(sess: aiohttp.ClientSession, procedure: str, payload: dict | None = None):
    """GET one tRPC procedure with an optional ``input`` payload."""
    url = f"{WARERA_API_BASE}/{procedure}"
    params = {"input": json.dumps(payload)} if payload is not None else None
    headers = {"x-api-key": WARERA_API_KEY} if WARERA_API_KEY else {}
    async with sess.get(
        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        return _unwrap(await resp.json())


def _parse_ts(ts: object) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


async def setup_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS war_tracked_defensive_battles (
            battle_id   TEXT PRIMARY KEY,
            region_id   TEXT NOT NULL,
            region_name TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS war_bunker_checks (
            battle_id   TEXT PRIMARY KEY,
            region_id   TEXT NOT NULL,
            region_name TEXT,
            check_at    TEXT NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS war_priority_tracking (
            war_id           TEXT PRIMARY KEY,
            enemy_country_id TEXT,
            priority_end_at  TEXT,
            notified_12h     INTEGER NOT NULL DEFAULT 0,
            notified_2h      INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.commit()


class WarRemindersCog(commands.Cog, name="war_reminders"):
    """Bunker-off, military-base and priority-ending reminders for Nigeria."""

    def __init__(self, bot: commands.Bot, db: aiosqlite.Connection) -> None:
        self.bot = bot
        self._db = db
        self.bunker_watch_loop.start()
        self.war_priority_loop.start()

    def cog_unload(self) -> None:
        self.bunker_watch_loop.cancel()
        self.war_priority_loop.cancel()

    # ── Feature 1: bunker-off reminder after a defensive battle ends ───────

    @tasks.loop(minutes=_POLL_MINUTES)
    async def bunker_watch_loop(self) -> None:
        try:
            await self._poll_defensive_battles()
        except Exception:
            logger.exception("war_reminders: bunker_watch_loop failed")
        try:
            await self._process_due_bunker_checks()
        except Exception:
            logger.exception("war_reminders: processing due bunker checks failed")

    @bunker_watch_loop.before_loop
    async def _before_bunker_watch_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_defensive_battles(self) -> None:
        async with aiohttp.ClientSession() as sess:
            try:
                battle_ids = await _get(
                    sess, "battle.getActiveBattlesByCountry", {"countryId": NIGERIA_COUNTRY_ID}
                )
            except Exception as exc:
                logger.warning("war_reminders: failed to fetch active battles: %s", exc)
                return
            if not isinstance(battle_ids, list):
                return

            # getActiveBattlesByCountry returns bare battle-id strings, not
            # full battle objects (confirmed live 2026-11-25) — each one
            # needs a battle.getById to learn its defender/region.
            current: dict[str, tuple[str, str]] = {}
            for battle_id in battle_ids:
                if not isinstance(battle_id, str):
                    continue
                try:
                    b = await _get(sess, "battle.getById", {"battleId": battle_id})
                except Exception as exc:
                    logger.warning(
                        "war_reminders: failed to fetch battle %s: %s", battle_id, exc
                    )
                    continue
                if not isinstance(b, dict):
                    continue
                defender = b.get("defender") or {}
                if defender.get("country") != NIGERIA_COUNTRY_ID:
                    continue
                region_id = defender.get("region")
                if not region_id:
                    continue
                current[battle_id] = (region_id, "")

            tracked: dict[str, tuple[str, str]] = {}
            async with self._db.execute(
                "SELECT battle_id, region_id, region_name FROM war_tracked_defensive_battles"
            ) as cur:
                async for row in cur:
                    tracked[row[0]] = (row[1], row[2] or "")

            new_ids = set(current) - set(tracked)
            ended_ids = set(tracked) - set(current)

            if new_ids:
                region_names = await self._region_names(
                    sess, {current[bid][0] for bid in new_ids}
                )
                for bid in new_ids:
                    region_id, _ = current[bid]
                    region_name = region_names.get(region_id, region_id)
                    await self._db.execute(
                        "INSERT OR REPLACE INTO war_tracked_defensive_battles "
                        "(battle_id, region_id, region_name) VALUES (?, ?, ?)",
                        (bid, region_id, region_name),
                    )
                    logger.info(
                        "war_reminders: tracking new defensive battle %s in %s",
                        bid, region_name,
                    )

            if ended_ids:
                check_at = (datetime.now(timezone.utc) + _BUNKER_CHECK_DELAY).isoformat()
                for bid in ended_ids:
                    region_id, region_name = tracked[bid]
                    await self._db.execute(
                        "INSERT OR REPLACE INTO war_bunker_checks "
                        "(battle_id, region_id, region_name, check_at) VALUES (?, ?, ?, ?)",
                        (bid, region_id, region_name, check_at),
                    )
                    await self._db.execute(
                        "DELETE FROM war_tracked_defensive_battles WHERE battle_id = ?", (bid,)
                    )
                    logger.info(
                        "war_reminders: defensive battle %s ended in %s — bunker check queued for %s",
                        bid, region_name or region_id, check_at,
                    )

            if new_ids or ended_ids:
                await self._db.commit()

    async def _region_names(
        self, sess: aiohttp.ClientSession, region_ids: set[str]
    ) -> dict[str, str]:
        if not region_ids:
            return {}
        try:
            regions = await _get(sess, "region.getRegionsObject")
        except Exception as exc:
            logger.warning("war_reminders: failed to fetch regions: %s", exc)
            return {}
        if not isinstance(regions, dict):
            return {}
        return {
            rid: (regions[rid].get("name") or rid)
            for rid in region_ids
            if isinstance(regions.get(rid), dict)
        }

    async def _process_due_bunker_checks(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        due: list[tuple[str, str, str]] = []
        async with self._db.execute(
            "SELECT battle_id, region_id, region_name FROM war_bunker_checks WHERE check_at <= ?",
            (now_iso,),
        ) as cur:
            async for row in cur:
                due.append((row[0], row[1], row[2] or row[1]))
        if not due:
            return

        async with aiohttp.ClientSession() as sess:
            for battle_id, region_id, region_name in due:
                try:
                    still_active = await self._bunker_still_active(sess, region_id)
                except Exception as exc:
                    logger.warning(
                        "war_reminders: bunker status check failed for %s: %s", region_name, exc
                    )
                    still_active = False
                if still_active:
                    try:
                        await self._send_bunker_reminder(region_name, battle_id)
                    except Exception:
                        logger.exception(
                            "war_reminders: failed to send bunker reminder for %s", region_name
                        )
                await self._db.execute(
                    "DELETE FROM war_bunker_checks WHERE battle_id = ?", (battle_id,)
                )
        await self._db.commit()

    async def _bunker_still_active(self, sess: aiohttp.ClientSession, region_id: str) -> bool:
        try:
            data = await _get(
                sess,
                "upgrade.getUpgradeByTypeAndEntity",
                {"upgradeType": "bunker", "regionId": region_id},
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return False
            raise
        if not isinstance(data, dict):
            return False
        return (data.get("status") or "").lower() == "active" and (data.get("level") or 0) > 0

    async def _send_bunker_reminder(self, region_name: str, battle_id: str) -> None:
        channel = self.bot.get_channel(_BUNKER_CHANNEL_ID)
        if channel is None:
            logger.warning("war_reminders: bunker channel %d not found", _BUNKER_CHANNEL_ID)
            return
        embed = discord.Embed(
            title="🛡️ Bunker nog actief",
            description=(
                f"Het gevecht in **{region_name}** is een uur geleden afgelopen, "
                "maar de bunker daar staat nog aan. Zet hem uit om olie te besparen."
            ),
            colour=discord.Colour.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        await channel.send(embed=embed)
        logger.info("war_reminders: sent bunker-off reminder for %s", region_name)

    # ── Features 2 & 3: war-priority warnings ───────────────────────────────

    @tasks.loop(minutes=_POLL_MINUTES * 2)
    async def war_priority_loop(self) -> None:
        try:
            await self._poll_war_priorities()
        except Exception:
            logger.exception("war_reminders: war_priority_loop failed")

    @war_priority_loop.before_loop
    async def _before_war_priority_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_war_priorities(self) -> None:
        tracked_ids: set[str] = set()
        async with self._db.execute("SELECT war_id FROM war_priority_tracking") as cur:
            async for row in cur:
                tracked_ids.add(row[0])

        async with aiohttp.ClientSession() as sess:
            discovered_ids: set[str] = set()
            try:
                battles = await _get(
                    sess, "battle.getBattles", {"countryId": NIGERIA_COUNTRY_ID, "limit": 100}
                )
            except Exception as exc:
                logger.warning("war_reminders: failed to fetch recent battles: %s", exc)
                battles = None
            if isinstance(battles, dict):
                for item in battles.get("items") or []:
                    if isinstance(item, dict) and item.get("war"):
                        discovered_ids.add(item["war"])

            for war_id in tracked_ids | discovered_ids:
                try:
                    await self._check_war(sess, war_id)
                except Exception:
                    logger.exception("war_reminders: failed to check war %s", war_id)
            await self._db.commit()

    async def _check_war(self, sess: aiohttp.ClientSession, war_id: str) -> None:
        try:
            war = await _get(sess, "war.getById", {"warId": war_id})
        except Exception as exc:
            logger.warning("war_reminders: failed to fetch war %s: %s", war_id, exc)
            return
        if not isinstance(war, dict):
            return

        if not war.get("isActive"):
            await self._db.execute(
                "DELETE FROM war_priority_tracking WHERE war_id = ?", (war_id,)
            )
            return

        priority_end_raw = war.get("priorityEndAt")
        priority_end = _parse_ts(priority_end_raw)
        if priority_end is None:
            return

        attacker_country = (war.get("attacker") or {}).get("country")
        defender_country = (war.get("defender") or {}).get("country")
        if attacker_country == NIGERIA_COUNTRY_ID:
            enemy_country_id = defender_country
        elif defender_country == NIGERIA_COUNTRY_ID:
            enemy_country_id = attacker_country
        else:
            return  # Nigeria isn't actually a side of this war — not relevant.

        async with self._db.execute(
            "SELECT priority_end_at, notified_12h, notified_2h FROM war_priority_tracking "
            "WHERE war_id = ?",
            (war_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None or row[0] != priority_end_raw:
            # New war, or the API reports a new priorityEndAt (the window got
            # extended/replaced) — (re)start the one-shot notification flags.
            notified_12h, notified_2h = False, False
            await self._db.execute(
                "INSERT OR REPLACE INTO war_priority_tracking "
                "(war_id, enemy_country_id, priority_end_at, notified_12h, notified_2h) "
                "VALUES (?, ?, ?, 0, 0)",
                (war_id, enemy_country_id, priority_end_raw),
            )
        else:
            notified_12h, notified_2h = bool(row[1]), bool(row[2])

        hours_left = (priority_end - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_left < 0:
            return  # Priority window already lapsed; wait for the API to report the new one.

        if hours_left <= _BASE_WARNING_HOURS and not notified_12h:
            try:
                await self._send_base_reminder(sess, enemy_country_id, priority_end)
            except Exception:
                logger.exception("war_reminders: failed to send base reminder for war %s", war_id)
            await self._db.execute(
                "UPDATE war_priority_tracking SET notified_12h = 1 WHERE war_id = ?", (war_id,)
            )

        if hours_left <= _PRIORITY_END_WARNING_HOURS and not notified_2h:
            try:
                await self._send_priority_ending_notice(enemy_country_id, priority_end)
            except Exception:
                logger.exception(
                    "war_reminders: failed to send priority-ending notice for war %s", war_id
                )
            await self._db.execute(
                "UPDATE war_priority_tracking SET notified_2h = 1 WHERE war_id = ?", (war_id,)
            )

    async def _enemy_country_name(
        self, sess: aiohttp.ClientSession, enemy_country_id: str | None
    ) -> str:
        if not enemy_country_id:
            return "de vijand"
        try:
            country = await _get(sess, "country.getCountryById", {"countryId": enemy_country_id})
        except Exception:
            return enemy_country_id
        if isinstance(country, dict):
            return country.get("name") or enemy_country_id
        return enemy_country_id

    async def _find_bordering_region(
        self, sess: aiohttp.ClientSession, enemy_country_id: str | None
    ) -> tuple[str, str] | None:
        if not enemy_country_id:
            return None
        try:
            regions = await _get(sess, "region.getRegionsObject")
        except Exception as exc:
            logger.warning("war_reminders: failed to fetch regions: %s", exc)
            return None
        if not isinstance(regions, dict):
            return None

        enemy_region_ids = {
            rid for rid, r in regions.items()
            if isinstance(r, dict) and r.get("country") == enemy_country_id
        }
        if not enemy_region_ids:
            return None

        for rid, r in regions.items():
            if not isinstance(r, dict) or r.get("country") != NIGERIA_COUNTRY_ID:
                continue
            if enemy_region_ids & set(r.get("neighbors") or []):
                return rid, r.get("name") or rid
        return None

    async def _send_base_reminder(
        self, sess: aiohttp.ClientSession, enemy_country_id: str | None, priority_end: datetime
    ) -> None:
        channel = self.bot.get_channel(_WAR_PRIORITY_CHANNEL_ID)
        if channel is None:
            logger.warning("war_reminders: war-priority channel %d not found", _WAR_PRIORITY_CHANNEL_ID)
            return

        enemy_name = await self._enemy_country_name(sess, enemy_country_id)
        border = await self._find_bordering_region(sess, enemy_country_id)
        if border:
            _region_id, region_name = border
            location_line = f"Zet een militaire basis aan in **{region_name}** (grenst aan {enemy_name})."
        else:
            location_line = (
                f"Zet een militaire basis aan in een regio die grenst aan **{enemy_name}**."
                " (Geen grenzende Nigeriaanse regio automatisch gevonden, handmatig checken.)"
            )

        priority_end_str = discord.utils.format_dt(priority_end, style="R")
        embed = discord.Embed(
            title="⚔️ Prioriteit loopt bijna af",
            description=(
                f"De prioriteit in de oorlog met **{enemy_name}** loopt af {priority_end_str}. "
                "Vanaf dat moment kan iedereen weer aanvallen.\n\n"
                "Een militaire basis duurt 12 uur om aan te zetten, dus begin nu zodat hij "
                "klaar staat tegen die tijd.\n\n"
                f"{location_line}"
            ),
            colour=discord.Colour.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        await channel.send(embed=embed)
        logger.info("war_reminders: sent base reminder for war vs %s", enemy_name)

    async def _send_priority_ending_notice(
        self, enemy_country_id: str | None, priority_end: datetime
    ) -> None:
        channel = self.bot.get_channel(_WAR_PRIORITY_CHANNEL_ID)
        if channel is None:
            logger.warning("war_reminders: war-priority channel %d not found", _WAR_PRIORITY_CHANNEL_ID)
            return

        async with aiohttp.ClientSession() as sess:
            enemy_name = await self._enemy_country_name(sess, enemy_country_id)

        priority_end_str = discord.utils.format_dt(priority_end, style="R")
        embed = discord.Embed(
            title="⏰ Prioriteit eindigt binnenkort",
            description=(
                f"De prioriteit in de oorlog met **{enemy_name}** eindigt {priority_end_str}. "
                "Vanaf dat moment kan iedereen weer aanvallen."
            ),
            colour=discord.Colour.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await channel.send(embed=embed)
        logger.info("war_reminders: sent priority-ending notice for war vs %s", enemy_name)


async def setup(bot: commands.Bot, db: aiosqlite.Connection) -> WarRemindersCog:
    await setup_schema(db)
    cog = WarRemindersCog(bot, db)
    await bot.add_cog(cog)
    return cog
