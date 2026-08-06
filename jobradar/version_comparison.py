"""Aggregation and reporting for isolated historical pipeline comparisons."""
from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from pathlib import Path


def alternating_order(pair_index: int) -> tuple[str, str]:
    return ("baseline", "candidate") if pair_index % 2 == 0 else ("candidate", "baseline")


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(runs: Sequence[dict]) -> dict:
    totals = [float(run["total_elapsed"]) for run in runs]
    first = [float(run["time_to_first_job"]) for run in runs if run.get("time_to_first_job") is not None]
    assessed = [int(run.get("pipeline_stats", {}).get("llm_assessed", 0)) for run in runs]
    evaluation_tasks = [
        int(
            run.get("pipeline_stats", {}).get("evaluation_tasks")
            or run.get("pipeline_stats", {}).get("new_saved", 0)
        )
        for run in runs
    ]
    overlap = [
        float(run.get("pipeline_stats", {}).get("overlap_elapsed", 0))
        for run in runs
        if "overlap_elapsed" in run.get("pipeline_stats", {})
    ]
    worker_counts = {int(run.get("assessment_workers", 1)) for run in runs}
    return {
        "runs": len(runs),
        "assessment_workers": next(iter(worker_counts)) if len(worker_counts) == 1 else sorted(worker_counts),
        "total_mean": statistics.mean(totals),
        "total_p50": statistics.median(totals),
        "total_p95": _percentile(totals, 0.95),
        "first_job_mean": statistics.mean(first) if first else None,
        "first_job_p50": statistics.median(first) if first else None,
        "first_job_p95": _percentile(first, 0.95),
        "result_count_mean": statistics.mean(len(run["result_keys"]) for run in runs),
        "assessed_jobs_mean": statistics.mean(assessed),
        "assessed_jobs_per_minute_mean": statistics.mean(
            count / elapsed * 60 for count, elapsed in zip(assessed, totals) if elapsed > 0
        ),
        "evaluation_tasks_mean": statistics.mean(evaluation_tasks),
        "evaluation_peak_inflight_mean": statistics.mean(
            int(run.get("pipeline_stats", {}).get("evaluation_peak_inflight") or (1 if tasks else 0))
            for run, tasks in zip(runs, evaluation_tasks)
        ),
        "evaluation_failed_total": sum(
            int(run.get("pipeline_stats", {}).get("evaluation_failed", 0)) for run in runs
        ),
        "overlap_mean": statistics.mean(overlap) if overlap else None,
        "llm_calls_mean": statistics.mean(int(run["llm_calls"]) for run in runs),
        "tokens_in_mean": statistics.mean(int(run["tokens_in"]) for run in runs),
        "tokens_out_mean": statistics.mean(int(run["tokens_out"]) for run in runs),
    }


def _improvement(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None or baseline == 0:
        return None
    return (baseline - candidate) / baseline * 100


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 1.0


def build_version_comparison(
    baseline_runs: Sequence[dict],
    candidate_runs: Sequence[dict],
    *,
    baseline_ref: str,
    candidate_ref: str,
) -> dict:
    if not baseline_runs or not candidate_runs:
        raise ValueError("Both baseline and candidate runs are required")
    if len(baseline_runs) != len(candidate_runs):
        raise ValueError("Baseline and candidate run counts must match")
    dataset_hashes = {run["dataset_hash"] for run in [*baseline_runs, *candidate_runs]}
    profile_hashes = {run["profile_hash"] for run in [*baseline_runs, *candidate_runs]}
    if len(dataset_hashes) != 1:
        raise ValueError("Version runs used different frozen datasets")
    if len(profile_hashes) != 1:
        raise ValueError("Version runs used different CV profiles")
    providers = {run["provider"] for run in [*baseline_runs, *candidate_runs]}
    models = {run["model"] for run in [*baseline_runs, *candidate_runs]}
    if len(providers) != 1 or len(models) != 1:
        raise ValueError("Version runs used different LLM configurations")
    replay_contracts = {
        json.dumps(run.get("replay_contract", {}), ensure_ascii=False, sort_keys=True)
        for run in [*baseline_runs, *candidate_runs]
    }
    if len(replay_contracts) != 1:
        raise ValueError("Version runs used different recorded replay contracts")

    baseline = _summary(baseline_runs)
    candidate = _summary(candidate_runs)
    pair_jaccard = [
        _jaccard(base["result_keys"], current["result_keys"])
        for base, current in zip(baseline_runs, candidate_runs)
    ]
    exact_pairs = sum(
        set(base["result_keys"]) == set(current["result_keys"])
        for base, current in zip(baseline_runs, candidate_runs)
    )
    return {
        "schema_version": 1,
        "baseline_ref": baseline_ref,
        "candidate_ref": candidate_ref,
        "dataset_hash": next(iter(dataset_hashes)),
        "profile_hash": next(iter(profile_hashes)),
        "provider": baseline_runs[0]["provider"],
        "model": baseline_runs[0]["model"],
        "timing_mode": baseline_runs[0].get("timing_mode", "unknown"),
        "speed_factor": baseline_runs[0].get("speed_factor", 1.0),
        "replay_contract": json.loads(next(iter(replay_contracts))),
        "baseline": baseline,
        "candidate": candidate,
        "improvement_percent": {
            "total_mean": _improvement(baseline["total_mean"], candidate["total_mean"]),
            "total_p50": _improvement(baseline["total_p50"], candidate["total_p50"]),
            "total_p95": _improvement(baseline["total_p95"], candidate["total_p95"]),
            "first_job_mean": _improvement(baseline["first_job_mean"], candidate["first_job_mean"]),
            "first_job_p50": _improvement(baseline["first_job_p50"], candidate["first_job_p50"]),
            "assessed_jobs_per_minute_mean": (
                (candidate["assessed_jobs_per_minute_mean"] - baseline["assessed_jobs_per_minute_mean"])
                / baseline["assessed_jobs_per_minute_mean"]
                * 100
                if baseline["assessed_jobs_per_minute_mean"]
                else None
            ),
        },
        "result_comparison": {
            "exact_pairs": exact_pairs,
            "pair_count": len(pair_jaccard),
            "jaccard_mean": statistics.mean(pair_jaccard),
            "jaccard_min": min(pair_jaccard),
        },
    }


def _display(value: object, digits: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown_report(controlled: dict, real: dict | None) -> str:
    lines = [
        "# JobRadar pipeline comparison",
        "",
        "## Controlled scheduling replay",
        "",
        f"- Dataset: `{controlled['dataset_id']}`",
        f"- Dataset hash: `{controlled['dataset_hash']}`",
        f"- Result equivalent: `{str(controlled['result_equivalent']).lower()}`",
        "",
        "| Metric | Serial | Streaming |",
        "|---|---:|---:|",
        f"| Mean total elapsed | {_display(controlled['serial']['total_mean'])} s | {_display(controlled['streaming']['total_mean'])} s |",
        f"| P50 total elapsed | {_display(controlled['serial']['total_p50'])} s | {_display(controlled['streaming']['total_p50'])} s |",
        f"| Mean first result | {_display(controlled['serial']['first_result_mean'])} s | {_display(controlled['streaming']['first_result_mean'])} s |",
        f"| Mean overlap | {_display(controlled['serial']['overlap_mean'])} s | {_display(controlled['streaming']['overlap_mean'])} s |",
    ]
    if real is None:
        lines.extend(
            [
                "",
                "## Historical real-LLM comparison",
                "",
                "Not run. Enable it explicitly with `--real-llm`.",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "",
            "## Historical real-LLM comparison",
            "",
            f"- Baseline: `{real['baseline_ref']}`",
            f"- Candidate: `{real['candidate_ref']}`",
            f"- Provider/model: `{real['provider']}/{real['model']}`",
            f"- Execution: historical serial ({real['baseline']['assessment_workers']} worker) versus current streaming ({real['candidate']['assessment_workers']} workers)",
            f"- Replay timing: `{real['timing_mode']}`, speed factor `{_display(real['speed_factor'])}`",
            f"- Mean result-set Jaccard: `{_display(real['result_comparison']['jaccard_mean'])}`",
            "",
            "### Recorded producer contract",
            "",
            f"- Source/role query events: `{real['replay_contract']['query_event_count']}` (empty after Python filter: `{real['replay_contract']['empty_query_event_count']}`)",
            f"- Non-empty candidate batches: `{real['replay_contract']['nonempty_batch_count']}`; candidates: `{real['replay_contract']['candidate_count']}`",
            f"- Producer elapsed: `{_display(real['replay_contract']['producer_finished_offset_seconds'])} s`; last candidate ready: `{_display(real['replay_contract']['last_candidate_ready_offset_seconds'])} s`; producer tail: `{_display(real['replay_contract']['producer_tail_seconds'])} s`",
            "- Both arms replay the same recorded arrival schedule. Empty-query time is preserved by later offsets and the producer tail.",
            "",
            "### Results",
            "",
            "| Metric | Baseline serial | Candidate parallel |",
            "|---|---:|---:|",
            f"| Mean total elapsed | {_display(real['baseline']['total_mean'])} s | {_display(real['candidate']['total_mean'])} s |",
            f"| P50 total elapsed | {_display(real['baseline']['total_p50'])} s | {_display(real['candidate']['total_p50'])} s |",
            f"| P95 total elapsed | {_display(real['baseline']['total_p95'])} s | {_display(real['candidate']['total_p95'])} s |",
            f"| Mean first job | {_display(real['baseline']['first_job_mean'])} s | {_display(real['candidate']['first_job_mean'])} s |",
            f"| Mean assessed jobs/min | {_display(real['baseline']['assessed_jobs_per_minute_mean'])} | {_display(real['candidate']['assessed_jobs_per_minute_mean'])} |",
            f"| Mean evaluation tasks | {_display(real['baseline']['evaluation_tasks_mean'], 1)} | {_display(real['candidate']['evaluation_tasks_mean'], 1)} |",
            f"| Mean peak evaluation concurrency | {_display(real['baseline']['evaluation_peak_inflight_mean'], 1)} | {_display(real['candidate']['evaluation_peak_inflight_mean'], 1)} |",
            f"| Mean scrape/evaluation overlap | {_display(real['baseline']['overlap_mean'])} s | {_display(real['candidate']['overlap_mean'])} s |",
            f"| Mean LLM calls | {_display(real['baseline']['llm_calls_mean'], 1)} | {_display(real['candidate']['llm_calls_mean'], 1)} |",
            f"| Mean input tokens | {_display(real['baseline']['tokens_in_mean'], 1)} | {_display(real['candidate']['tokens_in_mean'], 1)} |",
            f"| Mean output tokens | {_display(real['baseline']['tokens_out_mean'], 1)} | {_display(real['candidate']['tokens_out_mean'], 1)} |",
            f"| Mean result count | {_display(real['baseline']['result_count_mean'], 1)} | {_display(real['candidate']['result_count_mean'], 1)} |",
            "",
            "### Parallel gain",
            "",
            f"- Mean total elapsed reduction: `{_display(real['improvement_percent']['total_mean'])}%`",
            f"- P50 total elapsed reduction: `{_display(real['improvement_percent']['total_p50'])}%`",
            f"- P95 total elapsed reduction: `{_display(real['improvement_percent']['total_p95'])}%`",
            f"- Mean first-job reduction: `{_display(real['improvement_percent']['first_job_mean'])}%`",
            f"- Mean assessed-job throughput improvement: `{_display(real['improvement_percent']['assessed_jobs_per_minute_mean'])}%`",
        ]
    )
    return "\n".join(lines) + "\n"


def write_comparison_reports(
    output_directory: str | Path,
    *,
    controlled: dict,
    real: dict | None,
    baseline_runs: Sequence[dict] = (),
    candidate_runs: Sequence[dict] = (),
) -> tuple[Path, Path]:
    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "controlled": controlled,
        "real": real,
        "baseline_runs": list(baseline_runs),
        "candidate_runs": list(candidate_runs),
    }
    json_path = output_dir / "version_comparison.json"
    markdown_path = output_dir / "version_comparison.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown_report(controlled, real), encoding="utf-8")
    return json_path, markdown_path
