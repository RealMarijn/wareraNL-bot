"""Mercenary contract tracking DB methods.

See database/schema.sql (mercenary_contracts, mercenary_mu_agg) for the
why behind the baseline/delta design — battleRanking.getRanking only ever
gives a cumulative running total per (mu, battle, side), never a per-contract
figure, so completion has to be derived from deltas tracked over time by
rijksoverheid_web/app/services/mercenary_tracker.py.
"""

from __future__ import annotations

from typing import Optional

import aiosqlite


class MercenaryContractsMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    # ------------------------------------------------------------------ #
    # Discovery (new won auctions)
    # ------------------------------------------------------------------ #

    async def get_known_mercenary_contract_ids(
        self, auction_ids: list[str]
    ) -> set[str]:
        """Return the subset of *auction_ids* already stored."""
        if not auction_ids:
            return set()
        ph = ",".join("?" * len(auction_ids))
        async with self._conn.execute(
            f"SELECT auction_id FROM mercenary_contracts WHERE auction_id IN ({ph})",
            auction_ids,
        ) as cur:
            return {row[0] async for row in cur}

    async def get_last_cumulative_for_mu_battle_side(
        self, mu_id: str, battle_id: str, side: str
    ) -> Optional[float]:
        """Most recent cumulative-damage reading we have for this (mu, battle,
        side) from a PRIOR contract, used as the next contract's baseline."""
        async with self._conn.execute(
            """
            SELECT last_seen_cumulative FROM mercenary_contracts
             WHERE mu_id = ? AND battle_id = ? AND for_country_side = ?
               AND last_seen_cumulative IS NOT NULL
             ORDER BY first_seen_at DESC
             LIMIT 1
            """,
            (mu_id, battle_id, side),
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None

    async def insert_mercenary_contract(
        self,
        auction_id: str,
        mu_id: str,
        battle_id: str,
        for_country: Optional[str],
        for_country_side: str,
        minimum_damage: float,
        budget: Optional[float],
        current_per_k: Optional[float],
        professionals_only: Optional[bool],
        created_at: Optional[str],
        expires_at: Optional[str],
        bid_at: Optional[str],
        baseline_damage: float,
        now_iso: str,
    ) -> None:
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO mercenary_contracts
                (auction_id, mu_id, battle_id, for_country, for_country_side,
                 minimum_damage, budget, current_per_k, professionals_only,
                 created_at, expires_at, bid_at, baseline_damage,
                 last_seen_cumulative, last_checked_at, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                auction_id, mu_id, battle_id, for_country, for_country_side,
                minimum_damage, budget, current_per_k,
                1 if professionals_only else 0,
                created_at, expires_at, bid_at, baseline_damage,
                baseline_damage, now_iso, now_iso,
            ),
        )

    # ------------------------------------------------------------------ #
    # Progress tracking
    # ------------------------------------------------------------------ #

    async def get_pending_battle_sides(self) -> list[tuple[str, str]]:
        """Distinct (battle_id, side) pairs with at least one contract still
        being tracked (not completed, not given up on)."""
        rows: list[tuple[str, str]] = []
        async with self._conn.execute(
            """
            SELECT DISTINCT battle_id, for_country_side
              FROM mercenary_contracts
             WHERE completed_at IS NULL AND tracking_ended_at IS NULL
            """
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return rows

    async def get_pending_contracts_for_battle_side(
        self, battle_id: str, side: str
    ) -> list[dict]:
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT auction_id, mu_id, baseline_damage, minimum_damage, created_at
              FROM mercenary_contracts
             WHERE battle_id = ? AND for_country_side = ?
               AND completed_at IS NULL AND tracking_ended_at IS NULL
            """,
            (battle_id, side),
        ) as cur:
            async for row in cur:
                rows.append({
                    "auction_id": row[0],
                    "mu_id": row[1],
                    "baseline_damage": float(row[2] or 0),
                    "minimum_damage": float(row[3] or 0),
                    "created_at": row[4],
                })
        return rows

    async def update_mercenary_contract_progress(
        self, auction_id: str, last_seen_cumulative: float, now_iso: str
    ) -> None:
        await self._conn.execute(
            """
            UPDATE mercenary_contracts
               SET last_seen_cumulative = ?, last_checked_at = ?
             WHERE auction_id = ?
            """,
            (last_seen_cumulative, now_iso, auction_id),
        )

    async def complete_mercenary_contract(
        self, auction_id: str, completed_damage: float, now_iso: str
    ) -> None:
        await self._conn.execute(
            """
            UPDATE mercenary_contracts
               SET completed_damage = ?, completed_at = ?
             WHERE auction_id = ? AND completed_at IS NULL
            """,
            (completed_damage, now_iso, auction_id),
        )

    async def end_mercenary_contract_tracking(
        self, auction_id: str, now_iso: str
    ) -> None:
        """Give up on a contract that never reached minimum_damage (battle
        ended, or it's been pending too long)."""
        await self._conn.execute(
            """
            UPDATE mercenary_contracts
               SET tracking_ended_at = ?
             WHERE auction_id = ? AND completed_at IS NULL AND tracking_ended_at IS NULL
            """,
            (now_iso, auction_id),
        )

    async def commit_mercenary_contracts(self) -> None:
        await self._conn.commit()

    async def get_stale_pending_contracts(self, older_than_iso: str) -> list[str]:
        """auction_ids still pending whose created_at is older than the cutoff
        (candidates to give up tracking on)."""
        rows: list[str] = []
        async with self._conn.execute(
            """
            SELECT auction_id FROM mercenary_contracts
             WHERE completed_at IS NULL AND tracking_ended_at IS NULL
               AND created_at IS NOT NULL AND created_at < ?
            """,
            (older_than_iso,),
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    # ------------------------------------------------------------------ #
    # Retention / rollup
    # ------------------------------------------------------------------ #

    async def fold_old_mercenary_contracts(self, cutoff_iso: str) -> int:
        """Fold finalized contracts older than *cutoff_iso* into the
        mercenary_mu_agg rollup, then delete the detail rows. Returns the
        number of rows folded. Only finalized (completed or abandoned)
        contracts are eligible — in-progress ones are never pruned."""
        async with self._conn.execute(
            """
            SELECT mu_id,
                   COUNT(*)                                            AS n,
                   COUNT(completed_at)                                 AS n_completed,
                   COALESCE(SUM(budget), 0)                            AS money,
                   COALESCE(SUM(CASE WHEN completed_at IS NOT NULL
                                      THEN completed_damage ELSE 0 END), 0) AS damage,
                   COALESCE(SUM(CASE WHEN completed_at IS NOT NULL
                                      THEN (julianday(completed_at) - julianday(created_at)) * 86400.0
                                      ELSE 0 END), 0)                  AS completion_seconds
              FROM mercenary_contracts
             WHERE (completed_at IS NOT NULL OR tracking_ended_at IS NOT NULL)
               AND COALESCE(completed_at, tracking_ended_at) < ?
             GROUP BY mu_id
            """,
            (cutoff_iso,),
        ) as cur:
            agg_rows = [row async for row in cur]

        for mu_id, n, n_completed, money, damage, completion_seconds in agg_rows:
            await self._conn.execute(
                """
                INSERT INTO mercenary_mu_agg
                    (mu_id, contracts_count, completed_count, total_money,
                     total_damage, total_completion_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mu_id) DO UPDATE SET
                    contracts_count = contracts_count + excluded.contracts_count,
                    completed_count = completed_count + excluded.completed_count,
                    total_money = total_money + excluded.total_money,
                    total_damage = total_damage + excluded.total_damage,
                    total_completion_seconds = total_completion_seconds + excluded.total_completion_seconds
                """,
                (mu_id, n, n_completed, money, damage, completion_seconds),
            )

        await self._conn.execute(
            """
            DELETE FROM mercenary_contracts
             WHERE (completed_at IS NOT NULL OR tracking_ended_at IS NOT NULL)
               AND COALESCE(completed_at, tracking_ended_at) < ?
            """,
            (cutoff_iso,),
        )
        await self._conn.commit()
        return sum(n for _, n, *_ in agg_rows)

    # ------------------------------------------------------------------ #
    # Page queries
    # ------------------------------------------------------------------ #

    async def get_mercenary_mu_stats(
        self, limit: int = 200, min_contracts: int = 1
    ) -> list[dict]:
        """Per-MU rollup combining live (unpruned) rows with the folded
        historical aggregate, joined against known_mus for display names.

        Returns dicts: mu_id, mu_name, avatar_url, contracts_count,
        completed_count, total_money, avg_damage, avg_completion_seconds,
        avg_rate_per_k (money / (avg_damage/1000), only when both are known).
        """
        rows: list[dict] = []
        async with self._conn.execute(
            """
            WITH live AS (
                SELECT mu_id,
                       COUNT(*)                                            AS n,
                       COUNT(completed_at)                                 AS n_completed,
                       COALESCE(SUM(budget), 0)                            AS money,
                       COALESCE(SUM(CASE WHEN completed_at IS NOT NULL
                                          THEN completed_damage ELSE 0 END), 0) AS damage,
                       COALESCE(SUM(CASE WHEN completed_at IS NOT NULL
                                          THEN (julianday(completed_at) - julianday(created_at)) * 86400.0
                                          ELSE 0 END), 0)                  AS completion_seconds
                  FROM mercenary_contracts
                 WHERE completed_at IS NOT NULL OR tracking_ended_at IS NOT NULL
                 GROUP BY mu_id
            ),
            agg AS (
                SELECT mu_id, contracts_count, completed_count, total_money,
                       total_damage, total_completion_seconds
                  FROM mercenary_mu_agg
            ),
            combined AS (
                SELECT
                    COALESCE(live.mu_id, agg.mu_id)                                      AS mu_id,
                    COALESCE(live.n, 0) + COALESCE(agg.contracts_count, 0)                AS contracts_count,
                    COALESCE(live.n_completed, 0) + COALESCE(agg.completed_count, 0)      AS completed_count,
                    COALESCE(live.money, 0) + COALESCE(agg.total_money, 0)                AS total_money,
                    COALESCE(live.damage, 0) + COALESCE(agg.total_damage, 0)              AS total_damage,
                    COALESCE(live.completion_seconds, 0) + COALESCE(agg.total_completion_seconds, 0)
                                                                                            AS total_completion_seconds
                FROM live
                FULL OUTER JOIN agg ON agg.mu_id = live.mu_id
            )
            SELECT combined.mu_id,
                   COALESCE(km.mu_name, combined.mu_id) AS mu_name,
                   km.avatar_url,
                   combined.contracts_count,
                   combined.completed_count,
                   combined.total_money,
                   combined.total_damage,
                   combined.total_completion_seconds
              FROM combined
              LEFT JOIN known_mus km ON km.mu_id = combined.mu_id
             WHERE combined.contracts_count >= ?
             ORDER BY combined.contracts_count DESC
             LIMIT ?
            """,
            (min_contracts, limit),
        ) as cur:
            async for row in cur:
                (mu_id, mu_name, avatar_url, n, n_completed,
                 money, damage, completion_seconds) = row
                n_completed = n_completed or 0
                avg_damage = (damage / n_completed) if n_completed else None
                avg_completion_s = (completion_seconds / n_completed) if n_completed else None
                avg_rate_per_k = (
                    (money / n_completed) / (avg_damage / 1000.0)
                    if n_completed and avg_damage else None
                )
                rows.append({
                    "mu_id": mu_id,
                    "mu_name": mu_name,
                    "avatar_url": avatar_url,
                    "contracts_count": n or 0,
                    "completed_count": n_completed,
                    "total_money": float(money or 0),
                    "avg_damage": avg_damage,
                    "avg_completion_seconds": avg_completion_s,
                    "avg_rate_per_k": avg_rate_per_k,
                })
        return rows

    async def get_mercenary_contracts_for_mu(
        self, mu_id: str, limit: int = 100
    ) -> list[dict]:
        """Recent (unpruned) contract rows for one MU, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT auction_id, battle_id, for_country, for_country_side,
                   minimum_damage, budget, current_per_k, created_at,
                   baseline_damage, last_seen_cumulative, completed_damage,
                   completed_at, tracking_ended_at
              FROM mercenary_contracts
             WHERE mu_id = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (mu_id, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "auction_id": row[0],
                    "battle_id": row[1],
                    "for_country": row[2],
                    "for_country_side": row[3],
                    "minimum_damage": float(row[4] or 0),
                    "budget": float(row[5]) if row[5] is not None else None,
                    "current_per_k": float(row[6]) if row[6] is not None else None,
                    "created_at": row[7],
                    "baseline_damage": float(row[8] or 0),
                    "last_seen_cumulative": float(row[9]) if row[9] is not None else None,
                    "completed_damage": float(row[10]) if row[10] is not None else None,
                    "completed_at": row[11],
                    "tracking_ended_at": row[12],
                })
        return rows
