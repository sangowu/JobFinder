"""Capture post-Python-filter JobSpy batches for deterministic replay.

This command uses an isolated cache database so capture-time prefiltering never
reads or writes the production JobRadar cache. It intentionally stops before
title/coarse/JD LLM assessment.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture frozen JobRadar candidate batches")
    parser.add_argument("--profile", required=True, help="CVProfile JSON file")
    parser.add_argument("--location", required=True)
    parser.add_argument("--role", action="append", dest="roles", help="Repeat for each role; defaults to profile roles")
    parser.add_argument("--output", required=True, help="Output .json replay dataset")
    parser.add_argument("--cache-db", help="Isolated capture cache; defaults next to output")
    parser.add_argument("--indeed-limit", type=int, default=200)
    parser.add_argument("--linkedin-limit", type=int, default=30)
    parser.add_argument("--hours-old", type=int, default=72)
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path = Path(args.cache_db).resolve() if args.cache_db else output_path.with_name(
        f"{output_path.stem}_capture_cache.db"
    )
    os.environ["CACHE_DB_PATH"] = str(cache_path)

    from jobradar.pipeline_benchmark import canonical_hash
    from jobradar.schemas import CVProfile
    from jobradar.scraping import stream_scrape_source_batch_events
    from jobradar.search_prefilter import prefilter_jobs

    profile = CVProfile.model_validate_json(Path(args.profile).read_text(encoding="utf-8"))
    roles = args.roles or profile.preferred_roles
    if not roles:
        raise SystemExit("No roles were provided and CVProfile.preferred_roles is empty")

    started_at = time.perf_counter()
    seen_urls: set[str] = set()
    seen_dedup_keys: set[str] = set()
    batches: list[dict] = []
    query_events: list[dict] = []
    for batch_index, event in enumerate(
        stream_scrape_source_batch_events(
            roles=roles,
            location=args.location,
            cb=print,
            limit_per_query=args.indeed_limit,
            linkedin_limit_per_role=args.linkedin_limit,
            hours_old=args.hours_old,
        )
    ):
        scraped_batch = list(event.jobs)
        filtered = prefilter_jobs(
            scraped_batch,
            seen_urls,
            print,
            profile,
            run_id="capture",
            seen_dedup_keys=seen_dedup_keys,
        )
        batch_id = f"batch-{batch_index + 1:04d}"
        ready_offset = round(time.perf_counter() - started_at, 6)
        candidates: list[dict] = []
        for job, _content, _expires_at in filtered.pending:
            candidate = copy.deepcopy(job)
            candidate["capture_meta"] = {
                "batch_id": batch_id,
                "source": event.source,
                "role": event.role,
                "observed_offset_seconds": ready_offset,
            }
            candidates.append(candidate)
        query_events.append(
            {
                "batch_id": batch_id,
                "source": event.source,
                "role": event.role,
                "request_started_offset_seconds": round(event.request_started_at - started_at, 6),
                "request_finished_offset_seconds": round(event.request_finished_at - started_at, 6),
                "scrape_elapsed_seconds": round(event.scrape_elapsed, 6),
                "emitted_offset_seconds": round(event.emitted_at - started_at, 6),
                "ready_offset_seconds": ready_offset,
                "raw_count": event.raw_count,
                "url_unique_count": event.unique_count,
                "date_filtered_count": len(scraped_batch),
                "python_filtered_count": len(candidates),
            }
        )
        if not candidates:
            continue
        batches.append(
            {
                "batch_id": batch_id,
                "source": event.source,
                "role": event.role,
                "request_started_offset_seconds": round(event.request_started_at - started_at, 6),
                "request_finished_offset_seconds": round(event.request_finished_at - started_at, 6),
                "scrape_elapsed_seconds": round(event.scrape_elapsed, 6),
                "emitted_offset_seconds": round(event.emitted_at - started_at, 6),
                "ready_offset_seconds": ready_offset,
                "raw_count": event.raw_count,
                "url_unique_count": event.unique_count,
                "date_filtered_count": len(scraped_batch),
                "python_filtered_count": len(candidates),
                "jobs": candidates,
            }
        )

    producer_finished_offset = round(time.perf_counter() - started_at, 6)
    source_finished_offsets: dict[str, float] = {}
    for event in query_events:
        source = str(event["source"])
        source_finished_offsets[source] = max(
            source_finished_offsets.get(source, 0.0),
            float(event["request_finished_offset_seconds"]),
        )

    dataset_payload = {
        "schema_version": 2,
        "dataset_id": f"jobradar-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "location": args.location,
        "roles": roles,
        "profile_hash": canonical_hash(json.loads(profile.model_dump_json())),
        "producer_finished_offset_seconds": producer_finished_offset,
        "source_finished_offsets_seconds": source_finished_offsets,
        "query_events": query_events,
        "batches": batches,
    }
    output_path.write_text(json.dumps(dataset_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Captured {sum(len(batch['jobs']) for batch in batches)} candidates "
        f"in {len(batches)} non-empty batches from {len(query_events)} source/role queries "
        f"over {producer_finished_offset:.2f}s"
    )
    print(f"Dataset: {output_path}")
    print(f"Isolated capture cache: {cache_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
