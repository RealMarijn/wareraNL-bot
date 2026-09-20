"""Third-party "intel" RSS feed items DB methods.

Written every ~2 min by rijksoverheid_web/app/services/intel_feed.py (attack vectors, border
activity, alliance changes, etc. from a third-party WarEra-watching service); read by the
extension's whitelisted-only /api/ext/intel/feed endpoint (see
rijksoverheid_web/app/routers/extension_intel.py).

Each item's guid is "intel-event-<N>" with N a monotonically increasing integer — used directly as
the primary key and as the cursor extension clients page from (fetch everything with event_id >
their last-seen cursor), rather than storing/comparing the opaque guid string.
"""

from __future__ import annotations

from typing import Iterable

import aiosqlite


class IntelFeedMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def save_intel_feed_items(
        self,
        rows: Iterable[tuple[int, str, str, str, str, str]],
        fetched_at: str,
    ) -> int:
        """Insert new items = (event_id, title, link, category, description, pub_date).

        INSERT OR IGNORE — items are immutable once published (the feed has no "edited" concept),
        and this is called with every item the feed currently returns each poll, so re-inserting
        ones already stored is expected and just a no-op per row.
        """
        payload = [
            (event_id, title, link, category, description, pub_date, fetched_at)
            for event_id, title, link, category, description, pub_date in rows
        ]
        if not payload:
            return 0
        cur = await self._conn.executemany(
            "INSERT OR IGNORE INTO intel_feed_items "
            "(event_id, title, link, category, description, pub_date, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def get_intel_feed_items_since(self, cursor: int, limit: int = 100) -> list[dict]:
        """Items with event_id > cursor, oldest first (so a client applying them in order ends up
        with the correct latest cursor = the last item's event_id), capped at *limit* per call so a
        client that's been offline a long time doesn't get a single huge response."""
        out: list[dict] = []
        async with self._conn.execute(
            "SELECT event_id, title, link, category, description, pub_date "
            "FROM intel_feed_items WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
            (cursor, limit),
        ) as cur:
            async for row in cur:
                out.append({
                    "event_id": row[0], "title": row[1], "link": row[2],
                    "category": row[3], "description": row[4], "pub_date": row[5],
                })
        return out

    async def get_latest_intel_feed_event_id(self) -> int:
        """Highest event_id currently stored, or 0 if none yet — used to give a brand-new extension
        install a starting cursor of "now" rather than replaying the whole backlog on first poll."""
        async with self._conn.execute("SELECT MAX(event_id) FROM intel_feed_items") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def prune_intel_feed_items(self, older_than_iso: str) -> int:
        """Delete items published before *older_than_iso* (ISO-8601 UTC) — keeps the table bounded
        without needing a row-count cap; called once per poll in intel_feed.py."""
        cur = await self._conn.execute(
            "DELETE FROM intel_feed_items WHERE pub_date < ?", (older_than_iso,)
        )
        await self._conn.commit()
        return cur.rowcount or 0
