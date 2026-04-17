"""
Standalone dedup verification — wraps jobfinder.dedup_check for CLI use.

Usage:
    uv run python scripts/verify_dedup.py
    uv run python scripts/verify_dedup.py --db jobfinder_test_cache.db
    uv run python scripts/verify_dedup.py --threshold 0.9
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def _short(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[:n] + "..."


def main(db_path: str = "jobfinder_cache.db") -> dict:
    path = Path(db_path)
    if not path.exists():
        print(f"[error] DB not found: {path.resolve()}")
        return {}

    # Point the cache module at the requested DB
    os.environ["CACHE_DB_PATH"] = str(path)

    import sqlite3
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    all_keys = [r[0] for r in con.execute(
        "SELECT dedup_key FROM job_cache WHERE assessment IS NOT NULL"
    )]
    con.close()

    if not all_keys:
        print("No assessed jobs in DB.")
        return {}

    from jobfinder.dedup_check import run_dedup_check
    result = run_dedup_check(all_keys)

    sep = "-" * 60
    print(f"\n{sep}")
    print(f"  Dedup verification  --  {path.name}  ({result['total']} assessed jobs)")
    print(sep)

    def badge(n: int, warn: bool = False) -> str:
        if n == 0:
            return "[PASS]"
        return f"[WARN  {n}]" if warn else f"[FAIL  {n}]"

    print(f"\nL1 | Exact dedup_key duplicates          {badge(result['l1'])}")
    print(f"L2 | URL appearing in multiple dedup_keys  {badge(result['l2'], warn=True)}")

    for item in result.get("l2_items", [])[:10]:
        print(f"     {_short(item['url'])}")
        for k in item["dedup_keys"]:
            print(f"       -> {k}")

    label = "ALL PASS" if not (result["l1"] or result["l2"]) else "ISSUES FOUND"
    print(f"\n{sep}")
    print(f"  Result: {label}  |  L1={result['l1']}  L2={result['l2']}")
    print(sep + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify job cache for duplicates")
    parser.add_argument("--db", default="jobfinder_cache.db")
    args = parser.parse_args()
    main(db_path=args.db)
