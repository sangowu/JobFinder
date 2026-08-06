"""Aggregation and reporting for the historical/model/worker benchmark matrix."""
from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from pathlib import Path


def rotated_order(arms: Sequence[str], block_index: int) -> tuple[str, ...]:
    """Rotate arms so wall-clock position is distributed across measured blocks."""
    if not arms:
        raise ValueError("At least one benchmark arm is required")
    offset = block_index % len(arms)
    return tuple([*arms[offset:], *arms[:offset]])


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def summarize_runs(runs: Sequence[dict]) -> dict:
    if not runs:
        raise ValueError("At least one successful run is required")
    totals = [float(run["total_elapsed"]) for run in runs]
    first_jobs = [float(run["time_to_first_job"]) for run in runs if run.get("time_to_first_job") is not None]
    result_counts = [len(run.get("result_keys", ())) for run in runs]
    assessed_counts = [int(run.get("pipeline_stats", {}).get("llm_assessed", 0)) for run in runs]
    task_counts = [int(run.get("pipeline_stats", {}).get("evaluation_tasks", 0)) for run in runs]
    return {
        "runs": len(runs),
        "total_mean": statistics.mean(totals),
        "total_p50": statistics.median(totals),
        "total_p95": _percentile(totals, 0.95),
        "total_min": min(totals),
        "total_max": max(totals),
        "first_job_mean": _mean(first_jobs),
        "first_job_p50": statistics.median(first_jobs) if first_jobs else None,
        "first_job_p95": _percentile(first_jobs, 0.95),
        "visible_jobs_mean": statistics.mean(result_counts),
        "visible_jobs_per_minute_mean": statistics.mean(
            count / elapsed * 60 for count, elapsed in zip(result_counts, totals) if elapsed > 0
        ),
        "assessed_jobs_mean": statistics.mean(assessed_counts),
        "assessed_jobs_per_minute_mean": statistics.mean(
            count / elapsed * 60 for count, elapsed in zip(assessed_counts, totals) if elapsed > 0
        ),
        "evaluation_tasks_mean": statistics.mean(task_counts),
        "evaluation_tasks_per_minute_mean": statistics.mean(
            count / elapsed * 60 for count, elapsed in zip(task_counts, totals) if elapsed > 0
        ),
        "llm_calls_mean": statistics.mean(int(run.get("llm_calls", 0)) for run in runs),
        "tokens_in_mean": statistics.mean(int(run.get("tokens_in", 0)) for run in runs),
        "tokens_out_mean": statistics.mean(int(run.get("tokens_out", 0)) for run in runs),
        "evaluation_peak_inflight_mean": statistics.mean(
            int(run.get("pipeline_stats", {}).get("evaluation_peak_inflight", 0)) for run in runs
        ),
        "evaluation_failed_total": sum(
            int(run.get("pipeline_stats", {}).get("evaluation_failed", 0)) for run in runs
        ),
    }


def _relative_change(reference: float | None, other: float | None, *, lower_is_better: bool) -> float | None:
    if reference is None or other is None or reference == 0:
        return None
    numerator = reference - other if lower_is_better else other - reference
    return numerator / reference * 100


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 1.0


def _contrast(reference_runs: Sequence[dict], other_runs: Sequence[dict]) -> dict:
    reference = summarize_runs(reference_runs)
    other = summarize_runs(other_runs)
    paired = min(len(reference_runs), len(other_runs))
    overlaps = [
        _jaccard(reference_runs[index].get("result_keys", ()), other_runs[index].get("result_keys", ()))
        for index in range(paired)
    ]
    return {
        "reference": reference,
        "other": other,
        "change_percent": {
            "total_mean": _relative_change(reference["total_mean"], other["total_mean"], lower_is_better=True),
            "total_p50": _relative_change(reference["total_p50"], other["total_p50"], lower_is_better=True),
            "total_p95": _relative_change(reference["total_p95"], other["total_p95"], lower_is_better=True),
            "first_job_mean": _relative_change(
                reference["first_job_mean"], other["first_job_mean"], lower_is_better=True
            ),
            "assessed_jobs_per_minute_mean": _relative_change(
                reference["assessed_jobs_per_minute_mean"],
                other["assessed_jobs_per_minute_mean"],
                lower_is_better=False,
            ),
        },
        "result_comparison": {
            "paired_runs": paired,
            "jaccard_mean": _mean(overlaps),
            "jaccard_min": min(overlaps) if overlaps else None,
            "exact_pairs": sum(
                set(reference_runs[index].get("result_keys", ()))
                == set(other_runs[index].get("result_keys", ()))
                for index in range(paired)
            ),
        },
    }


def build_matrix_report(
    runs_by_model: dict[str, dict[str, list[dict]]],
    *,
    model_names: dict[str, str],
    baseline_ref: str,
    candidate_ref: str,
    failed_runs: Sequence[dict] = (),
) -> dict:
    successful = [run for arms in runs_by_model.values() for runs in arms.values() for run in runs]
    if not successful:
        raise ValueError("No successful benchmark runs")
    dataset_hashes = {run["dataset_hash"] for run in successful}
    profile_hashes = {run["profile_hash"] for run in successful}
    providers = {run["provider"] for run in successful}
    if len(dataset_hashes) != 1 or len(profile_hashes) != 1 or len(providers) != 1:
        raise ValueError("Matrix runs must share one dataset, profile, and provider")

    summaries: dict[str, dict[str, dict]] = {}
    within_model: dict[str, dict[str, dict]] = {}
    for model_key, arms in runs_by_model.items():
        summaries[model_key] = {arm: summarize_runs(runs) for arm, runs in arms.items() if runs}
        baseline_runs = arms.get("baseline", [])
        within_model[model_key] = {
            arm: _contrast(baseline_runs, runs)
            for arm, runs in arms.items()
            if arm != "baseline" and baseline_runs and runs
        }

    cross_model: dict[str, dict] = {}
    worker_count: dict[str, dict] = {}
    for model_key, arms in runs_by_model.items():
        if arms.get("current-3w") and arms.get("current-5w"):
            worker_count[model_key] = _contrast(arms["current-3w"], arms["current-5w"])
    model_keys = list(model_names)
    if len(model_keys) >= 2:
        reference_key, replacement_key = model_keys[:2]
        common_arms = set(runs_by_model.get(reference_key, {})) & set(runs_by_model.get(replacement_key, {}))
        cross_model = {
            arm: _contrast(runs_by_model[reference_key][arm], runs_by_model[replacement_key][arm])
            for arm in sorted(common_arms)
            if runs_by_model[reference_key][arm] and runs_by_model[replacement_key][arm]
        }

    interaction: dict | None = None
    if len(model_keys) >= 2:
        first = within_model.get(model_keys[0], {}).get("current-3w")
        second = within_model.get(model_keys[1], {}).get("current-3w")
        if first and second:
            first_gain = first["change_percent"]["total_mean"]
            second_gain = second["change_percent"]["total_mean"]
            interaction = {
                "metric": "baseline_to_current_3w_total_mean_improvement_percent",
                "reference_model": first_gain,
                "replacement_model": second_gain,
                "difference_percentage_points": (
                    second_gain - first_gain if first_gain is not None and second_gain is not None else None
                ),
            }

    return {
        "schema_version": 1,
        "baseline_ref": baseline_ref,
        "candidate_ref": candidate_ref,
        "dataset_hash": next(iter(dataset_hashes)),
        "profile_hash": next(iter(profile_hashes)),
        "provider": next(iter(providers)),
        "models": model_names,
        "summaries": summaries,
        "within_model_contrasts": within_model,
        "cross_model_contrasts": cross_model,
        "worker_count_contrasts": worker_count,
        "model_concurrency_interaction": interaction,
        "failed_runs": list(failed_runs),
        "measurement_limits": {
            "sdk_internal_retries": "not observable from the current google-genai response surface",
            "http_429": "counted only when it escapes SDK retries and fails the worker process",
            "cost": "not calculated; provider billing metadata is not returned by these calls",
        },
    }


def _fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_matrix_markdown(report: dict) -> str:
    lines = [
        "# JobRadar historical/model/worker benchmark matrix",
        "",
        f"- Historical baseline: `{report['baseline_ref']}`",
        f"- Current candidate: `{report['candidate_ref']}`",
        f"- Provider: `{report['provider']}`",
        f"- Dataset hash: `{report['dataset_hash']}`",
        f"- Profile hash: `{report['profile_hash']}`",
        "",
    ]
    arm_labels = {"baseline": "09b20c0 serial", "current-3w": "current 3-worker", "current-5w": "current 5-worker"}
    for model_key, model_name in report["models"].items():
        lines.extend(
            [
                f"## {model_name}",
                "",
                "| Arm | Runs | Mean total | P50 | P95 | Mean first job | Assessed jobs/min | LLM calls | Tokens in | Visible jobs | Peak concurrency | Eval failures |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for arm, summary in report["summaries"].get(model_key, {}).items():
            lines.append(
                f"| {arm_labels.get(arm, arm)} | {summary['runs']} | {_fmt(summary['total_mean'])} s | "
                f"{_fmt(summary['total_p50'])} s | {_fmt(summary['total_p95'])} s | "
                f"{_fmt(summary['first_job_mean'])} s | {_fmt(summary['assessed_jobs_per_minute_mean'])} | "
                f"{_fmt(summary['llm_calls_mean'], 1)} | {_fmt(summary['tokens_in_mean'], 1)} | "
                f"{_fmt(summary['visible_jobs_mean'], 1)} | {_fmt(summary['evaluation_peak_inflight_mean'], 1)} | "
                f"{summary['evaluation_failed_total']} |"
            )
        lines.extend(["", "### Change from historical serial", "", "| Candidate | Mean total | P50 | P95 | Mean first job | Eval throughput | Mean Jaccard |", "|---|---:|---:|---:|---:|---:|---:|"])
        for arm, contrast in report["within_model_contrasts"].get(model_key, {}).items():
            change = contrast["change_percent"]
            lines.append(
                f"| {arm_labels.get(arm, arm)} | {_fmt(change['total_mean'])}% | {_fmt(change['total_p50'])}% | "
                f"{_fmt(change['total_p95'])}% | {_fmt(change['first_job_mean'])}% | "
                f"{_fmt(change['assessed_jobs_per_minute_mean'])}% | "
                f"{_fmt(contrast['result_comparison']['jaccard_mean'], 4)} |"
            )
        lines.append("")

    if report["cross_model_contrasts"]:
        first_key, second_key = list(report["models"])[:2]
        lines.extend(
            [
                "## Model replacement effect",
                "",
                f"Positive percentages mean `{report['models'][second_key]}` improved over `{report['models'][first_key]}`.",
                "",
                "| Same architecture arm | Mean total | P50 | P95 | Mean first job | Eval throughput |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for arm, contrast in report["cross_model_contrasts"].items():
            change = contrast["change_percent"]
            lines.append(
                f"| {arm_labels.get(arm, arm)} | {_fmt(change['total_mean'])}% | {_fmt(change['total_p50'])}% | "
                f"{_fmt(change['total_p95'])}% | {_fmt(change['first_job_mean'])}% | "
                f"{_fmt(change['assessed_jobs_per_minute_mean'])}% |"
            )
        lines.append("")

    if report["worker_count_contrasts"]:
        lines.extend(
            [
                "## Current 3-worker versus 5-worker",
                "",
                "Positive percentages mean 5-worker improved over 3-worker on the same current code and model.",
                "",
                "| Model | Mean total | P50 | P95 | Mean first job | Assessed throughput | Mean Jaccard |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for model_key, contrast in report["worker_count_contrasts"].items():
            change = contrast["change_percent"]
            lines.append(
                f"| {report['models'][model_key]} | {_fmt(change['total_mean'])}% | "
                f"{_fmt(change['total_p50'])}% | {_fmt(change['total_p95'])}% | "
                f"{_fmt(change['first_job_mean'])}% | {_fmt(change['assessed_jobs_per_minute_mean'])}% | "
                f"{_fmt(contrast['result_comparison']['jaccard_mean'], 4)} |"
            )
        lines.append("")

    interaction = report.get("model_concurrency_interaction")
    if interaction:
        lines.extend(
            [
                "## Model × concurrency interaction",
                "",
                f"- Reference-model baseline→3-worker mean-total improvement: `{_fmt(interaction['reference_model'])}%`",
                f"- Replacement-model baseline→3-worker mean-total improvement: `{_fmt(interaction['replacement_model'])}%`",
                f"- Difference: `{_fmt(interaction['difference_percentage_points'])}` percentage points",
                "",
            ]
        )
    lines.extend(
        [
            "## Measurement limits",
            "",
            "- google-genai internal retry attempts are not exposed by the current response surface.",
            "- A 429 is counted only if it escapes SDK retry handling and causes a worker-process failure.",
            "- Provider cost is not calculated because billing metadata is not returned by these calls.",
            "- LLM outputs are stochastic; result-set Jaccard is reported instead of assuming exact equivalence.",
            "",
            f"Failed worker runs: `{len(report['failed_runs'])}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_matrix_reports(output_directory: str | Path, report: dict, raw_runs: Sequence[dict]) -> tuple[Path, Path]:
    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {**report, "raw_runs": list(raw_runs)}
    json_path = output_dir / "pipeline_matrix.json"
    markdown_path = output_dir / "pipeline_matrix.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_matrix_markdown(report), encoding="utf-8")
    return json_path, markdown_path
