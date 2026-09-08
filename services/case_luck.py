"""Shared case-opening data fetching for luck calculations.

extract_case_counts() is now the primary path — user.getUserById's
stats.case1.byRarity / stats.case2.byRarity (confirmed live) already give
each player's full lifetime case-opening rarity counts directly, for the
cost of one (batchable) API call per player instead of paginating their
entire transaction.getPaginatedTransactions history. Used by the daily NL
sweep (cogs/tasks/luck.py), the global sweep (cogs/tasks/global_luck.py),
and the live single-player refresh in /geluk + /globalluck.

fetch_case_transactions()/merge_counts() are kept only for the one thing
byRarity genuinely can't do: "most recent N cases" (aantal_cases in /geluk
and /globalluck), which needs newest-first per-transaction ordering that a
lifetime cumulative counter doesn't carry. transaction.getPaginatedTransactions
pages newest-first (confirmed by direct API testing: page 1 starts at the
most recent transaction, each following page continues further back in
time) — that's what lets that one path stop early once it's collected
enough recent opens, rather than paging a player's whole history.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

RARITY_KEYS = ["mythic", "legendary", "epic", "rare", "uncommon", "common"]

logger = logging.getLogger("discord_bot")


def _unwrap(resp: object) -> object:
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v.get("data", v)
    return resp


def extract_case_counts(
    user_doc: object,
) -> Optional[tuple[dict[str, int], dict[str, int]]]:
    """Pull (normal_counts, elite_counts) out of one user.getUserById response.

    Lives at stats.case1.byRarity / stats.case2.byRarity (confirmed live) —
    case1 is normal cases, case2 is elite cases. Missing rarities (0 opens
    of that rarity) are simply absent from the API's dict rather than
    present with a 0, so every RARITY_KEYS entry is defaulted to 0 here to
    match the shape the rest of the codebase (rarity_json, luck-score math)
    expects. Returns None if stats/case1/case2 aren't present at all (e.g.
    the API call for this user failed upstream and returned something else).
    """
    data = _unwrap(user_doc)
    if not isinstance(data, dict):
        return None
    stats = data.get("stats")
    if not isinstance(stats, dict):
        return None
    case1 = stats.get("case1")
    case2 = stats.get("case2")
    if not isinstance(case1, dict) and not isinstance(case2, dict):
        return None
    case1_rarity = case1.get("byRarity") if isinstance(case1, dict) else None
    case2_rarity = case2.get("byRarity") if isinstance(case2, dict) else None
    normal_counts = {
        r: int((case1_rarity or {}).get(r, 0) or 0) for r in RARITY_KEYS
    }
    elite_counts = {
        r: int((case2_rarity or {}).get(r, 0) or 0) for r in RARITY_KEYS
    }
    return normal_counts, elite_counts


async def fetch_case_transactions(
    client,
    user_id: str,
    item_rarities: dict[str, str],
    *,
    cutoff_id: Optional[str] = None,
    max_cases: Optional[int] = None,
) -> tuple[dict[str, int], dict[str, int], Optional[str], int]:
    """Page openCase transactions for *user_id*, newest first.

    Stops as soon as a transaction's _id <= cutoff_id is reached. Pass
    cutoff_id=None for a full history fetch (e.g. a player's first scan).

    max_cases, if given, additionally stops once that many normal (non-elite)
    openings have been collected — for "most recent N cases" requests. Not
    meant to be combined with cutoff_id (mutually exclusive use cases).

    Returns (normal_counts, elite_counts, newest_id_seen, total_fetched).
    newest_id_seen is the _id of the first (newest) transaction encountered,
    or None if the player has no openCase transactions at all.
    """
    normal_counts: dict[str, int] = {r: 0 for r in RARITY_KEYS}
    elite_counts: dict[str, int] = {r: 0 for r in RARITY_KEYS}
    cursor: Optional[str] = None
    newest_id: Optional[str] = None
    total_fetched = 0
    limit_reached = False

    while True:
        payload: dict = {"userId": user_id, "transactionType": "openCase", "limit": 100}
        if cursor:
            payload["cursor"] = cursor
        try:
            raw = await client.get(
                "/transaction.getPaginatedTransactions",
                params={"input": json.dumps(payload)},
            )
        except Exception:
            logger.warning("fetch_case_transactions: request failed for %s", user_id)
            break

        data = raw
        if isinstance(raw, dict):
            inner = raw.get("result", raw)
            data = inner.get("data", inner) if isinstance(inner, dict) else raw
        items = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("transactions") or []
            cursor = data.get("nextCursor") or data.get("cursor")
        elif isinstance(data, list):
            items = data
            cursor = None

        stop = False
        for tx in items:
            if not isinstance(tx, dict):
                continue
            tx_id = str(tx.get("_id") or "")
            if cutoff_id and tx_id and tx_id <= cutoff_id:
                stop = True
                break
            if newest_id is None and tx_id:
                newest_id = tx_id  # first item on the first page is the newest
            opened_case = tx.get("itemCode", "")
            is_elite = item_rarities.get(opened_case) == "mythic"
            received = tx.get("item") or {}
            item_code = (
                received.get("code") if isinstance(received, dict) else received
            ) or ""
            rarity = item_rarities.get(item_code, "common")
            if is_elite:
                elite_counts[rarity] = elite_counts.get(rarity, 0) + 1
            else:
                normal_counts[rarity] = normal_counts.get(rarity, 0) + 1
                if max_cases is not None and sum(normal_counts.values()) >= max_cases:
                    limit_reached = True
                    break
            total_fetched += 1

        if not cursor or not items or stop or limit_reached:
            break

    return normal_counts, elite_counts, newest_id, total_fetched


def merge_counts(base: dict[str, int], delta: dict[str, int]) -> dict[str, int]:
    """Add delta rarity counts onto a base counts dict (for incremental merges)."""
    merged = dict(base)
    for k, v in delta.items():
        merged[k] = merged.get(k, 0) + v
    return merged
