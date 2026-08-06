"""Run one frozen-data JobRadar version comparison in an isolated process.

This file intentionally delays all JobRadar imports until ``--checkout`` has
been inserted at the front of ``sys.path``. The same worker can therefore run a
historical worktree or the current uncommitted candidate checkout.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def _load_batches(path: Path) -> tuple[str, list[dict], str, float, dict]:
    dataset_metadata: dict = {}
    if path.suffix.lower() == ".jsonl":
        batches = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        dataset_id = path.stem
        producer_finished_offset = 0.0
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            batches = payload
            dataset_id = path.stem
            producer_finished_offset = 0.0
        else:
            batches = payload["batches"]
            dataset_id = str(payload.get("dataset_id") or path.stem)
            dataset_metadata = {
                key: copy.deepcopy(value)
                for key, value in payload.items()
                if key not in {"dataset_id", "batches"}
            }
            producer_finished_offset = float(payload.get("producer_finished_offset_seconds") or 0.0)
    normalized: list[dict] = []
    for index, batch in enumerate(batches):
        offset = float(batch.get("ready_offset_seconds", batch.get("ready_offset_ms", 0) / 1000))
        metadata = {
            key: copy.deepcopy(value)
            for key, value in batch.items()
            if key not in {"batch_id", "ready_offset_seconds", "ready_offset_ms", "jobs"}
        }
        normalized.append({
            "batch_id": str(batch.get("batch_id") or f"batch-{index + 1:04d}"),
            "ready_offset_seconds": offset,
            **metadata,
            "jobs": copy.deepcopy(batch["jobs"]),
        })
    producer_finished_offset = max(
        producer_finished_offset,
        max((float(batch["ready_offset_seconds"]) for batch in normalized), default=0.0),
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "dataset_id": dataset_id,
                **dataset_metadata,
                "producer_finished_offset_seconds": producer_finished_offset,
                "batches": normalized,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    query_events = list(dataset_metadata.get("query_events") or [])
    last_ready = max((float(batch["ready_offset_seconds"]) for batch in normalized), default=0.0)
    replay_contract = {
        "schema_version": dataset_metadata.get("schema_version"),
        "timing_source": "recorded source-role query schedule",
        "query_event_count": len(query_events),
        "empty_query_event_count": sum(
            int(event.get("python_filtered_count", 0)) == 0 for event in query_events
        ),
        "nonempty_batch_count": len(normalized),
        "candidate_count": sum(len(batch["jobs"]) for batch in normalized),
        "producer_finished_offset_seconds": producer_finished_offset,
        "last_candidate_ready_offset_seconds": last_ready,
        "producer_tail_seconds": max(0.0, producer_finished_offset - last_ready),
        "source_finished_offsets_seconds": copy.deepcopy(
            dataset_metadata.get("source_finished_offsets_seconds") or {}
        ),
        "query_events": copy.deepcopy(query_events),
    }
    return dataset_id, normalized, digest, producer_finished_offset, replay_contract


def _sleep_until(started_at: float, offset: float, speed_factor: float) -> None:
    target = started_at + offset / speed_factor
    remaining = target - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _source_kind(job: dict) -> str:
    source = str(job.get("source") or "").lower()
    return "linkedin" if "linkedin" in source else "indeed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated JobRadar checkout against frozen batches")
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--version-mode", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--timing", choices=("instant", "recorded"), default="recorded")
    parser.add_argument("--speed-factor", type=float, default=1.0)
    parser.add_argument("--assessment-workers", type=int, default=1)
    args = parser.parse_args()

    if args.speed_factor <= 0:
        parser.error("--speed-factor must be greater than zero")
    if not 1 <= args.assessment_workers <= 8:
        parser.error("--assessment-workers must be between 1 and 8")

    checkout = Path(args.checkout).resolve()
    dataset_path = Path(args.dataset).resolve()
    profile_path = Path(args.profile).resolve()
    database_path = Path(args.database).resolve()
    output_path = Path(args.output).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    os.environ["CACHE_DB_PATH"] = str(database_path)
    os.environ["LOG_FILE"] = ""
    sys.path.insert(0, str(checkout))

    from jobradar import agent, cache, scraping
    from jobradar.llm_backend import LLMConfig
    from jobradar.schemas import CVProfile, make_dedup_key
    from jobradar.telemetry import telemetry

    dataset_id, batches, dataset_hash, producer_finished_offset, replay_contract = _load_batches(dataset_path)
    profile_payload = json.loads(profile_path.read_text(encoding="utf-8"))
    profile = CVProfile.model_validate(profile_payload)
    profile_hash = hashlib.sha256(
        json.dumps(profile_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    llm = LLMConfig(provider=args.provider, model=args.model)

    # Do not write pipeline reports or use date-relative filtering during replay.
    agent.PipelineStats.write_report = lambda self, *unused_args, **unused_kwargs: ""
    if hasattr(scraping, "_filter_by_posted_date"):
        scraping._filter_by_posted_date = lambda jobs, *unused_args, **unused_kwargs: jobs

    replay_started_at = time.monotonic()

    if args.version_mode == "baseline":
        source_jobs: dict[str, list[dict]] = {"indeed": [], "linkedin": []}
        source_ready: dict[str, float] = {"indeed": 0.0, "linkedin": 0.0}
        for batch in batches:
            for job in batch["jobs"]:
                kind = _source_kind(job)
                source_jobs[kind].append(copy.deepcopy(job))
                source_ready[kind] = max(source_ready[kind], float(batch["ready_offset_seconds"]))
        query_events = replay_contract["query_events"]
        for event in query_events:
            kind = _source_kind({"source": event.get("source")})
            source_ready[kind] = max(
                source_ready[kind],
                float(event.get("ready_offset_seconds") or event.get("request_finished_offset_seconds") or 0.0),
            )
        if source_ready:
            last_source = max(source_ready, key=source_ready.get)
            source_ready[last_source] = max(source_ready[last_source], producer_finished_offset)

        def _replay_source(kind: str):
            def _run(*unused_args, **unused_kwargs):
                if args.timing == "recorded":
                    _sleep_until(replay_started_at, source_ready[kind], args.speed_factor)
                return copy.deepcopy(source_jobs[kind])

            return _run

        scraping.scrape_indeed_jobspy_multi = _replay_source("indeed")
        scraping.scrape_linkedin_jobspy_multi = _replay_source("linkedin")
    else:
        def _replay_stream(*unused_args, **kwargs):
            stats = kwargs.get("stats")
            for batch in batches:
                if args.timing == "recorded":
                    _sleep_until(replay_started_at, float(batch["ready_offset_seconds"]), args.speed_factor)
                jobs = copy.deepcopy(batch["jobs"])
                if stats is not None:
                    indeed_count = sum(_source_kind(job) == "indeed" for job in jobs)
                    linkedin_count = len(jobs) - indeed_count
                    stats.scraped_indeed += indeed_count
                    stats.scraped_linkedin += linkedin_count
                    stats.scraped_total += len(jobs)
                yield jobs
            if args.timing == "recorded":
                _sleep_until(replay_started_at, producer_finished_offset, args.speed_factor)

        agent.stream_scrape_source_batches = _replay_stream

    telemetry.reset()
    emitted_keys: list[str] = []
    first_job_at: float | None = None

    def _on_job(key: str) -> None:
        nonlocal first_job_at
        if first_job_at is None:
            first_job_at = time.monotonic()
        emitted_keys.append(key)

    started_at = time.monotonic()
    run_kwargs = {
        "llm": llm,
        "cv_hash": profile_hash,
        "on_job": _on_job,
        "force_refresh": True,
        "language": args.language,
        "limit_per_role": 1,
        "linkedin_limit_per_role": 1,
        "hours_old": None,
    }
    if args.version_mode == "candidate":
        run_kwargs["assessment_workers"] = args.assessment_workers
    keys, stats = agent.run_search(profile, args.location, **run_kwargs)
    finished_at = time.monotonic()

    module_metrics = telemetry.summarize_llm_by_step()
    tokens_in = sum(int(values["input_tokens"]) for values in module_metrics.values())
    tokens_out = sum(int(values["output_tokens"]) for values in module_metrics.values())
    llm_calls = sum(int(values["calls"]) for values in module_metrics.values())
    jobs = cache.get_jobs_by_keys(keys)
    job_results = [
        {
            "dedup_key": job.dedup_key,
            "score": job.effective_score,
            "is_relevant": job.is_effectively_relevant,
        }
        for job in jobs
    ]
    run_id = str(getattr(stats, "run_id", ""))
    input_jobs = [copy.deepcopy(job) for batch in batches for job in batch["jobs"]]
    input_keys = {
        make_dedup_key(str(job.get("company") or ""), str(job.get("title") or ""))
        for job in input_jobs
    }
    cached_jobs = {
        key: cached
        for key in input_keys
        if (cached := cache.get_job(key, language=args.language)) is not None
    }
    from jobradar.model_quality_audit import build_run_decisions

    audit_decisions = build_run_decisions(
        input_jobs,
        result_keys=keys,
        filter_events=cache.get_filter_events(run_id=run_id, limit=max(500, len(input_jobs) * 4)),
        cached_jobs=cached_jobs,
    )
    result = {
        "schema_version": 1,
        "version_mode": args.version_mode,
        "checkout": str(checkout),
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "profile_hash": profile_hash,
        "run_id": run_id,
        "provider": args.provider,
        "model": args.model,
        "timing_mode": args.timing,
        "speed_factor": args.speed_factor,
        "assessment_workers": 1 if args.version_mode == "baseline" else args.assessment_workers,
        "execution_policy": "historical-serial" if args.version_mode == "baseline" else "streaming-worker-pool",
        "replay_contract": replay_contract,
        "total_elapsed": finished_at - started_at,
        "time_to_first_job": first_job_at - started_at if first_job_at is not None else None,
        "result_keys": keys,
        "emitted_keys": emitted_keys,
        "jobs": job_results,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "llm_calls": llm_calls,
        "module_metrics": module_metrics,
        "pipeline_stats": stats.to_dict(),
        "audit_decisions": audit_decisions,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
