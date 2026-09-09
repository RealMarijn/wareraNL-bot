"""SQLite persistence for COPErator, the COPE alliance verification bot.

Own file (``database/cope.db``), same pattern as ``nigeria_bot/db.py`` — a
separately-deployed bot process gets its own small SQLite database rather
than sharing the main bot's ``database/external.db``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import aiosqlite

DB_PATH = "database/cope.db"


async def open_db(path: str = DB_PATH) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS identity_links (
            discord_user_id TEXT PRIMARY KEY,
            warera_user_id  TEXT NOT NULL,
            warera_username TEXT,
            country_id      TEXT,
            government_role TEXT,
            saved_at        TEXT NOT NULL
        )
    """)
    # Ticket deletions are scheduled with asyncio.sleep() in-process; this
    # table lets a restart mid-wait resume (or immediately run) any deletion
    # that was still pending, instead of silently losing it forever — same
    # reasoning as nigeria_bot/db.py's identical table.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_ticket_deletions (
            channel_id TEXT PRIMARY KEY,
            delete_at  TEXT NOT NULL
        )
    """)
    # Caches country_id -> the Discord role id used for that country's
    # nationality role, including ones auto-created at runtime for a country
    # not in cog.py's hardcoded COUNTRY_ROLE_IDS. Without this, a restart
    # would lose track of an auto-created role and create a duplicate the
    # next time that country is seen.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS country_roles (
            country_id TEXT PRIMARY KEY,
            role_id    TEXT NOT NULL,
            code       TEXT,
            name       TEXT
        )
    """)
    await conn.commit()
    return conn


async def save_link(
    conn: aiosqlite.Connection,
    discord_id: str,
    warera_id: str,
    *,
    username: Optional[str] = None,
    country_id: Optional[str] = None,
    government_role: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT OR REPLACE INTO identity_links
            (discord_user_id, warera_user_id, warera_username, country_id, government_role, saved_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (discord_id, warera_id, username, country_id, government_role, now),
    )
    await conn.commit()


async def get_link(conn: aiosqlite.Connection, discord_id: str) -> Optional[tuple]:
    """Return (warera_user_id, warera_username, country_id, government_role) or None."""
    async with conn.execute(
        "SELECT warera_user_id, warera_username, country_id, government_role "
        "FROM identity_links WHERE discord_user_id = ?",
        (discord_id,),
    ) as cur:
        row = await cur.fetchone()
        return tuple(row) if row is not None else None


async def get_all_links(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """Return all (discord_user_id, warera_user_id) pairs."""
    rows: list[tuple[str, str]] = []
    async with conn.execute(
        "SELECT discord_user_id, warera_user_id FROM identity_links"
    ) as cur:
        async for row in cur:
            rows.append((row[0], row[1]))
    return rows


async def get_all_links_full(conn: aiosqlite.Connection) -> list[tuple]:
    """Return all (discord_user_id, warera_user_id, warera_username, country_id, government_role, saved_at) rows."""
    rows: list[tuple] = []
    async with conn.execute(
        "SELECT discord_user_id, warera_user_id, warera_username, country_id, government_role, saved_at "
        "FROM identity_links ORDER BY saved_at DESC"
    ) as cur:
        async for row in cur:
            rows.append(tuple(row))
    return rows


async def add_pending_ticket_deletion(
    conn: aiosqlite.Connection, channel_id: str, delete_at: str
) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO pending_ticket_deletions (channel_id, delete_at) VALUES (?, ?)",
        (channel_id, delete_at),
    )
    await conn.commit()


async def remove_pending_ticket_deletion(conn: aiosqlite.Connection, channel_id: str) -> None:
    await conn.execute(
        "DELETE FROM pending_ticket_deletions WHERE channel_id = ?", (channel_id,)
    )
    await conn.commit()


async def get_pending_ticket_deletions(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    async with conn.execute(
        "SELECT channel_id, delete_at FROM pending_ticket_deletions"
    ) as cur:
        async for row in cur:
            rows.append((row[0], row[1]))
    return rows


async def save_country_role(
    conn: aiosqlite.Connection, country_id: str, role_id: str, code: str, name: str
) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO country_roles (country_id, role_id, code, name) VALUES (?, ?, ?, ?)",
        (country_id, role_id, code, name),
    )
    await conn.commit()


async def get_all_country_roles(conn: aiosqlite.Connection) -> list[tuple[str, str, str, str]]:
    """Return all (country_id, role_id, code, name) rows."""
    rows: list[tuple[str, str, str, str]] = []
    async with conn.execute(
        "SELECT country_id, role_id, code, name FROM country_roles"
    ) as cur:
        async for row in cur:
            rows.append(tuple(row))
    return rows
