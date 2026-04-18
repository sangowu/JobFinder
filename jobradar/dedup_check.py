"""Post-search dedup verification: L1 exact key / L2 URL conflict."""
from __future__ import annotations

import json
from collections import defaultdict

from jobradar import cache


def run_dedup_check(dedup_keys: list[str]) -> dict:
    """
    Run field-based dedup checks on a set of dedup_keys (current search results).
    L1: exact dedup_key duplicates (PRIMARY KEY guarantee, should always be 0).
    L2: same URL appearing under different dedup_keys (cross-source merge gap).
    Returns a dict suitable for JSON serialisation and SSE payload.
    """
    if not dedup_keys:
        return {"total": 0, "l1": 0, "l2": 0, "l2_items": []}

    jobs = cache.get_jobs_by_keys(dedup_keys)
    if not jobs:
        return {"total": 0, "l1": 0, "l2": 0, "l2_items": []}

    # ── L1: exact dedup_key duplicates ────────────────────────────────────────
    seen: dict[str, int] = defaultdict(int)
    for j in jobs:
        seen[j.dedup_key] += 1
    l1_items = [{"dedup_key": k, "count": v} for k, v in seen.items() if v > 1]

    # ── L2: same URL under different dedup_keys ───────────────────────────────
    url_to_keys: dict[str, list[str]] = defaultdict(list)
    for j in jobs:
        if j.url:
            url_to_keys[j.url].append(j.dedup_key)
        for src in (j.raw_sources or []):
            src_url = src.get("url", "")
            if src_url and src_url != j.url:
                url_to_keys[src_url].append(j.dedup_key)
    l2_items = [
        {"url": url, "dedup_keys": list(dict.fromkeys(keys))}
        for url, keys in url_to_keys.items()
        if len(set(keys)) > 1
    ]

    return {
        "total": len(jobs),
        "l1": len(l1_items),
        "l2": len(l2_items),
        "l2_items": l2_items[:10],
    }
