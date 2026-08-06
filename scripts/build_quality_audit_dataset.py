"""Build a deterministic 100-job quality-audit dataset from a seed replay and cache."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _key(job: dict) -> str:
    from jobradar.schemas import make_dedup_key

    return make_dedup_key(str(job.get("company") or ""), str(job.get("title") or ""))


def _source(url: str) -> str:
    return "linkedin.com" if "linkedin" in url.lower() else "indeed.ie"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a fixed-size model-quality audit dataset")
    parser.add_argument("--seed-dataset", required=True)
    parser.add_argument("--cache-db", default="data/jobradar_cache.db")
    parser.add_argument("--output", required=True)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--dataset-id", default="standard-100-v1")
    args = parser.parse_args()
    if args.size <= 0:
        parser.error("--size must be positive")

    seed_payload = json.loads(Path(args.seed_dataset).read_text(encoding="utf-8"))
    seed_jobs = [job for batch in seed_payload["batches"] for job in batch["jobs"]]
    selected: dict[str, dict] = {}
    selected_urls: set[str] = set()
    for job in seed_jobs:
        key = _key(job)
        url = str(job.get("url") or "")
        if key in selected or not url or url in selected_urls:
            continue
        selected[key] = job
        selected_urls.add(url)
    if len(selected) > args.size:
        parser.error("Seed dataset already exceeds requested size")

    connection = sqlite3.connect(Path(args.cache_db))
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT dedup_key, title, company, location, description_snippet, url,
                  date_posted, is_complete, assessment
           FROM job_cache
           WHERE length(description_snippet) >= 500"""
    ).fetchall()
    connection.close()
    pools: dict[str, list[tuple[str, dict]]] = {"prior_pass": [], "prior_reject": []}
    pool_urls = set(selected_urls)
    for row in sorted(rows, key=lambda item: item["dedup_key"]):
        if row["dedup_key"] in selected or not row["url"] or row["url"] in pool_urls:
            continue
        assessment = json.loads(row["assessment"]) if row["assessment"] else {}
        stratum = "prior_pass" if assessment.get("is_relevant", True) else "prior_reject"
        job = {
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "url": row["url"],
            "apply_url": row["url"],
            "source": _source(row["url"]),
            "is_complete": bool(row["is_complete"]),
            "description_snippet": row["description_snippet"],
            "date_posted": row["date_posted"] or "",
            "audit_sampling_stratum": stratum,
        }
        digest = hashlib.sha256(f"{args.dataset_id}|{row['dedup_key']}".encode()).hexdigest()
        pools[stratum].append((digest, job))
        pool_urls.add(row["url"])
    for pool in pools.values():
        pool.sort(key=lambda item: item[0])

    remaining = args.size - len(selected)
    targets = {"prior_pass": (remaining + 1) // 2, "prior_reject": remaining // 2}
    for stratum, target in targets.items():
        for _digest, job in pools[stratum][:target]:
            selected[_key(job)] = job
            selected_urls.add(str(job["url"]))
    if len(selected) < args.size:
        leftovers = sorted(
            [item for pool in pools.values() for item in pool if _key(item[1]) not in selected],
            key=lambda item: item[0],
        )
        for _digest, job in leftovers[: args.size - len(selected)]:
            selected[_key(job)] = job
            selected_urls.add(str(job["url"]))
    if len(selected) != args.size:
        raise RuntimeError(f"Could build only {len(selected)} unique jobs")
    if len(selected_urls) != args.size:
        raise RuntimeError(f"Dataset contains only {len(selected_urls)} unique URLs")

    jobs = list(selected.values())
    batches = [
        {"batch_id": f"audit-{index // 10 + 1:02d}", "ready_offset_seconds": 0, "jobs": jobs[index:index + 10]}
        for index in range(0, len(jobs), 10)
    ]
    payload = {
        "schema_version": 1,
        "dataset_id": args.dataset_id,
        "sampling": {
            "size": args.size,
            "seed_dataset": str(Path(args.seed_dataset).resolve()),
            "seed_jobs": len(seed_jobs),
            "augmentation": "deterministic balanced sample from prior cached pass/reject strata",
            "selection_warning": "Prior cache strata enrich boundary cases and are not population prevalence estimates.",
        },
        "batches": batches,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Dataset: {output}")
    print(f"Unique jobs: {len(jobs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
