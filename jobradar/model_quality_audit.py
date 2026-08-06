"""Model-quality audit aggregation and blind human-review package generation."""
from __future__ import annotations

import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path

from .schemas import make_dedup_key
from .version_matrix import summarize_runs

STAGE_ORDER = ("title_relevance", "coarse_filter", "seniority", "experience_gap", "jd_assessment", "final_match")


def flatten_dataset_jobs(payload: dict) -> list[dict]:
    jobs: list[dict] = []
    seen: set[str] = set()
    for batch in payload["batches"]:
        for job in batch["jobs"]:
            key = make_dedup_key(str(job.get("company") or ""), str(job.get("title") or ""))
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
    return jobs


def build_run_decisions(
    input_jobs: Sequence[dict],
    *,
    result_keys: Sequence[str],
    filter_events: Sequence[dict],
    cached_jobs: dict[str, object],
) -> list[dict]:
    """Build one terminal decision per frozen input job from an isolated run."""
    visible = set(result_keys)
    events_by_key: dict[str, list[dict]] = defaultdict(list)
    for event in filter_events:
        key = make_dedup_key(str(event.get("company") or ""), str(event.get("title") or ""))
        events_by_key[key].append(event)

    decisions: list[dict] = []
    for job in input_jobs:
        key = make_dedup_key(str(job.get("company") or ""), str(job.get("title") or ""))
        cached = cached_jobs.get(key)
        if key in visible:
            decision, terminal_stage, reason = "pass", "final_visible", "final recommendation is visible"
            details: dict = {}
        elif events_by_key.get(key):
            event = min(
                events_by_key[key],
                key=lambda item: STAGE_ORDER.index(item["stage"]) if item["stage"] in STAGE_ORDER else 999,
            )
            decision, terminal_stage = "reject", str(event["stage"])
            reason, details = str(event.get("reason") or ""), dict(event.get("details") or {})
        else:
            decision, terminal_stage, reason, details = "unknown", "unobserved", "no terminal event recorded", {}

        assessment = getattr(cached, "assessment", None)
        jd_profile = getattr(cached, "jd_profile", None)
        match_score = getattr(cached, "match_score", None)
        coarse_filter = getattr(cached, "coarse_filter", None)
        decisions.append(
            {
                "dedup_key": key,
                "title": str(job.get("title") or ""),
                "company": str(job.get("company") or ""),
                "location": str(job.get("location") or ""),
                "source": str(job.get("source") or ""),
                "decision": decision,
                "terminal_stage": terminal_stage,
                "reason": reason,
                "details": details,
                "coarse_filter": coarse_filter.model_dump(mode="json") if coarse_filter is not None else None,
                "jd_assessment": assessment.model_dump(mode="json") if assessment is not None else None,
                "jd_profile": jd_profile.model_dump(mode="json") if jd_profile is not None else None,
                "match_score": match_score.model_dump(mode="json") if match_score is not None else None,
            }
        )
    return decisions


def build_quality_report(runs_by_model: dict[str, list[dict]], model_names: dict[str, str]) -> dict:
    if set(runs_by_model) != set(model_names):
        raise ValueError("Run and model keys must match")
    summaries = {key: summarize_runs(runs) for key, runs in runs_by_model.items()}
    job_stats: dict[str, dict[str, dict]] = {}
    stage_counts: dict[str, dict[str, float]] = {}
    stability: dict[str, float] = {}
    reason_samples: dict[str, dict[str, list[str]]] = {}
    for model_key, runs in runs_by_model.items():
        per_job: dict[str, Counter] = defaultdict(Counter)
        per_job_reasons: dict[str, list[str]] = defaultdict(list)
        stage_totals: Counter = Counter()
        for run in runs:
            for item in run.get("audit_decisions", []):
                per_job[item["dedup_key"]][item["decision"]] += 1
                per_job[item["dedup_key"]][f"stage:{item['terminal_stage']}"] += 1
                stage_totals[item["terminal_stage"]] += 1
                reason = str(item.get("reason") or "").strip()
                if item["decision"] == "reject" and reason and reason not in per_job_reasons[item["dedup_key"]]:
                    per_job_reasons[item["dedup_key"]].append(reason)
        job_stats[model_key] = {
            key: {
                "pass_count": counts["pass"],
                "reject_count": counts["reject"],
                "unknown_count": counts["unknown"],
                "terminal_stage_counts": {
                    name.removeprefix("stage:"): value
                    for name, value in counts.items()
                    if name.startswith("stage:")
                },
            }
            for key, counts in per_job.items()
        }
        stage_counts[model_key] = {
            stage: count / len(runs) for stage, count in sorted(stage_totals.items())
        }
        reason_samples[model_key] = per_job_reasons
        stability[model_key] = statistics.mean(
            max(values["pass_count"], values["reject_count"], values["unknown_count"]) / len(runs)
            for values in job_stats[model_key].values()
        )

    first_key, second_key = list(model_names)[:2]
    all_keys = sorted(set(job_stats[first_key]) | set(job_stats[second_key]))
    disagreements = []
    for key in all_keys:
        first = job_stats[first_key].get(key, {})
        second = job_stats[second_key].get(key, {})
        delta = int(first.get("pass_count", 0)) - int(second.get("pass_count", 0))
        if delta:
            decisions = next(
                item
                for run in runs_by_model[first_key]
                for item in run.get("audit_decisions", [])
                if item["dedup_key"] == key
            )
            disagreements.append(
                {
                    "dedup_key": key,
                    "title": decisions["title"],
                    "company": decisions["company"],
                    f"{first_key}_pass_count": int(first.get("pass_count", 0)),
                    f"{second_key}_pass_count": int(second.get("pass_count", 0)),
                    "pass_count_delta": delta,
                    f"{first_key}_terminal_stage_counts": dict(first.get("terminal_stage_counts", {})),
                    f"{second_key}_terminal_stage_counts": dict(second.get("terminal_stage_counts", {})),
                    f"{first_key}_rejection_reasons": reason_samples[first_key].get(key, [])[:3],
                    f"{second_key}_rejection_reasons": reason_samples[second_key].get(key, [])[:3],
                }
            )
    disagreements.sort(key=lambda item: (-abs(item["pass_count_delta"]), item["dedup_key"]))

    paired = min(len(runs_by_model[first_key]), len(runs_by_model[second_key]))
    jaccards = []
    for index in range(paired):
        left = set(runs_by_model[first_key][index]["result_keys"])
        right = set(runs_by_model[second_key][index]["result_keys"])
        union = left | right
        jaccards.append(len(left & right) / len(union) if union else 1.0)
    reference = summaries[first_key]
    replacement = summaries[second_key]

    def _lower_is_better(reference_value: float, replacement_value: float) -> float:
        return (reference_value - replacement_value) / reference_value * 100 if reference_value else 0.0

    return {
        "schema_version": 1,
        "models": model_names,
        "runs_per_model": {key: len(runs) for key, runs in runs_by_model.items()},
        "summaries": summaries,
        "mean_terminal_stage_counts": stage_counts,
        "decision_stability": stability,
        "job_frequencies": job_stats,
        "disagreements": disagreements,
        "paired_result_jaccard_mean": statistics.mean(jaccards),
        "performance_change_percent": {
            "mean_total": _lower_is_better(reference["total_mean"], replacement["total_mean"]),
            "p95_total": _lower_is_better(reference["total_p95"], replacement["total_p95"]),
            "llm_calls": _lower_is_better(reference["llm_calls_mean"], replacement["llm_calls_mean"]),
            "tokens_in": _lower_is_better(reference["tokens_in_mean"], replacement["tokens_in_mean"]),
            "tokens_out": _lower_is_better(reference["tokens_out_mean"], replacement["tokens_out_mean"]),
            "evaluation_tasks": _lower_is_better(
                reference["evaluation_tasks_mean"], replacement["evaluation_tasks_mean"]
            ),
            "visible_jobs": _lower_is_better(reference["visible_jobs_mean"], replacement["visible_jobs_mean"]),
        },
        "ground_truth_status": "pending_two_human_blind_reviews",
        "acceptance_gates": {
            "recommended_recall_delta_min_percentage_points": -3,
            "stretch_false_reject_delta_max_percentage_points": 3,
            "false_pass_rate_must_not_increase": True,
            "decision_stability_must_not_decrease": True,
            "mean_latency_improvement_min_percent": 15,
        },
    }


def _blind_order(jobs: Sequence[dict], reviewer: str) -> list[dict]:
    return sorted(
        jobs,
        key=lambda job: hashlib.sha256(
            f"{reviewer}|{make_dedup_key(str(job.get('company') or ''), str(job.get('title') or ''))}".encode()
        ).hexdigest(),
    )


def write_blind_review_packages(dataset_payload: dict, output_directory: str | Path) -> tuple[Path, Path, Path]:
    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs = flatten_dataset_jobs(dataset_payload)
    manifest = {}
    for index, job in enumerate(sorted(jobs, key=lambda item: make_dedup_key(item.get("company", ""), item.get("title", ""))), 1):
        manifest[f"J{index:03d}"] = make_dedup_key(str(job.get("company") or ""), str(job.get("title") or ""))
    key_to_blind = {value: key for key, value in manifest.items()}

    paths = []
    fieldnames = [
        "blind_id", "title", "company", "location", "job_description",
        "human_decision", "confidence", "reason", "reviewer_notes",
    ]
    for reviewer in ("a", "b"):
        path = output_dir / f"reviewer_{reviewer}_blind.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for job in _blind_order(jobs, reviewer):
                key = make_dedup_key(str(job.get("company") or ""), str(job.get("title") or ""))
                writer.writerow(
                    {
                        "blind_id": key_to_blind[key],
                        "title": job.get("title", ""),
                        "company": job.get("company", ""),
                        "location": job.get("location", ""),
                        "job_description": job.get("description_snippet", ""),
                        "human_decision": "",
                        "confidence": "",
                        "reason": "",
                        "reviewer_notes": "",
                    }
                )
        paths.append(path)
    manifest_path = output_dir / "blind_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "README.md").write_text(
        """# Blind human review instructions

Assign `reviewer_a_blind.csv` and `reviewer_b_blind.csv` to two different people.
Reviewers must not open `blind_manifest.json` or the model report before finishing.

Fill every row using only these `human_decision` values:

- `recommend`: clearly worth applying to;
- `stretch`: plausible stretch application worth retaining;
- `reject`: should not be recommended;
- `insufficient`: the JD lacks enough information for a decision.

Use `confidence` values `high`, `medium`, or `low`, and record a short independent
reason. Do not discuss labels until both files are complete. The coordinator then
runs `scripts/score_model_quality_reviews.py`. Reviewer disagreements require a
separate adjudication pass before a final model-replacement decision.
""",
        encoding="utf-8",
    )
    return paths[0], paths[1], manifest_path


def render_quality_markdown(report: dict) -> str:
    lines = [
        "# Gemini model quality audit",
        "",
        f"- Ground truth: `{report['ground_truth_status']}`",
        f"- Paired final-result Jaccard: `{report['paired_result_jaccard_mean']:.4f}`",
        "",
        "| Model | Runs | Mean total | P50 | P95 | Mean visible jobs | Decision stability |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, name in report["models"].items():
        summary = report["summaries"][key]
        lines.append(
            f"| {name} | {summary['runs']} | {summary['total_mean']:.2f}s | {summary['total_p50']:.2f}s | "
            f"{summary['total_p95']:.2f}s | {summary['visible_jobs_mean']:.1f} | "
            f"{report['decision_stability'][key]:.3f} |"
        )
    model_keys = list(report["models"])
    lines.extend(
        [
            "",
            "## Replacement performance change",
            "",
            "Positive percentages mean the replacement model uses less time or work than the reference model.",
            "",
            "| Mean total | P95 total | LLM calls | Input tokens | Output tokens | Deep tasks | Visible jobs |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            "| " + " | ".join(
                f"{report['performance_change_percent'][metric]:.2f}%"
                for metric in (
                    "mean_total", "p95_total", "llm_calls", "tokens_in", "tokens_out",
                    "evaluation_tasks", "visible_jobs",
                )
            ) + " |",
            "",
            "## Mean terminal stage counts per 100 jobs",
            "",
            f"| Stage | {report['models'][model_keys[0]]} | {report['models'][model_keys[1]]} |",
            "|---|---:|---:|",
        ]
    )
    stages = sorted(
        set(report["mean_terminal_stage_counts"][model_keys[0]])
        | set(report["mean_terminal_stage_counts"][model_keys[1]])
    )
    for stage in stages:
        lines.append(
            f"| {stage} | {report['mean_terminal_stage_counts'][model_keys[0]].get(stage, 0):.1f} | "
            f"{report['mean_terminal_stage_counts'][model_keys[1]].get(stage, 0):.1f} |"
        )
    lines.extend(
        [
            "",
            "## Largest five-run pass-frequency disagreements",
            "",
            f"| Job | {report['models'][model_keys[0]]} | {report['models'][model_keys[1]]} |",
            "|---|---:|---:|",
        ]
    )
    for item in report["disagreements"][:15]:
        lines.append(
            f"| {item['company']} · {item['title']} | {item[f'{model_keys[0]}_pass_count']}/5 | "
            f"{item[f'{model_keys[1]}_pass_count']}/5 |"
        )
    lines.extend(
        [
            "",
            "## Important",
            "",
            "This report compares model behavior and performance. Recall, false-reject, and false-pass gates remain pending until two blinded human review files are completed and adjudicated.",
            "",
            f"Jobs with different five-run pass frequencies: `{len(report['disagreements'])}`",
            "",
        ]
    )
    return "\n".join(lines)


def score_blind_reviews(
    report: dict,
    manifest: dict[str, str],
    reviewer_a_rows: Sequence[dict],
    reviewer_b_rows: Sequence[dict],
    adjudication_rows: Sequence[dict] = (),
) -> dict:
    """Score majority model decisions where two blinded reviewers agree."""
    allowed = {"recommend", "stretch", "reject", "insufficient"}

    def _labels(rows: Sequence[dict]) -> dict[str, str]:
        result = {}
        for row in rows:
            blind_id = str(row.get("blind_id") or "").strip()
            decision = str(row.get("human_decision") or "").strip().lower()
            if blind_id and decision:
                if decision not in allowed:
                    raise ValueError(f"Invalid human_decision for {blind_id}: {decision}")
                result[blind_id] = decision
        return result

    labels_a, labels_b = _labels(reviewer_a_rows), _labels(reviewer_b_rows)
    expected = set(manifest)
    if set(labels_a) != expected or set(labels_b) != expected:
        raise ValueError("Both reviewers must label all 100 blind IDs")
    agreed = {blind_id: labels_a[blind_id] for blind_id in expected if labels_a[blind_id] == labels_b[blind_id]}
    disagreements = sorted(expected - set(agreed))
    agreement_rate = len(agreed) / len(expected)
    adjudicated = _labels(adjudication_rows)
    invalid_adjudication = set(adjudicated) - set(disagreements)
    if invalid_adjudication:
        raise ValueError(f"Adjudication contains non-disagreement IDs: {sorted(invalid_adjudication)}")
    ground_truth = {**agreed, **adjudicated}
    unresolved = sorted(expected - set(ground_truth))
    metrics: dict[str, dict] = {}
    for model_key, job_values in report["job_frequencies"].items():
        counts = Counter()
        for blind_id, human in ground_truth.items():
            if human == "insufficient":
                continue
            key = manifest[blind_id]
            model_pass = int(job_values[key]["pass_count"]) >= 3
            counts[f"{human}_total"] += 1
            if model_pass:
                counts[f"{human}_passed"] += 1

        def _rate(numerator: str, denominator: str) -> float | None:
            return counts[numerator] / counts[denominator] if counts[denominator] else None

        recommend_recall = _rate("recommend_passed", "recommend_total")
        stretch_recall = _rate("stretch_passed", "stretch_total")
        false_pass_rate = _rate("reject_passed", "reject_total")
        metrics[model_key] = {
            "recommend_recall": recommend_recall,
            "stretch_recall": stretch_recall,
            "stretch_false_reject_rate": 1 - stretch_recall if stretch_recall is not None else None,
            "false_pass_rate": false_pass_rate,
            "label_counts": dict(counts),
        }
    model_keys = list(report["models"])
    reference, replacement = model_keys[:2]

    def _delta(metric: str) -> float | None:
        left = metrics[reference][metric]
        right = metrics[replacement][metric]
        return (right - left) * 100 if left is not None and right is not None else None

    gates = report["acceptance_gates"]
    recommend_delta = _delta("recommend_recall")
    stretch_false_reject_delta = _delta("stretch_false_reject_rate")
    false_pass_delta = _delta("false_pass_rate")
    stability_delta = (
        report["decision_stability"][replacement] - report["decision_stability"][reference]
    ) * 100
    latency_improvement = report["performance_change_percent"]["mean_total"]
    gate_results = {
        "recommend_recall": (
            recommend_delta >= gates["recommended_recall_delta_min_percentage_points"]
            if recommend_delta is not None else None
        ),
        "stretch_false_reject": (
            stretch_false_reject_delta <= gates["stretch_false_reject_delta_max_percentage_points"]
            if stretch_false_reject_delta is not None else None
        ),
        "false_pass": false_pass_delta <= 0 if false_pass_delta is not None else None,
        "decision_stability": stability_delta >= 0,
        "mean_latency": latency_improvement >= gates["mean_latency_improvement_min_percent"],
    }
    return {
        "reviewer_agreement_rate": agreement_rate,
        "agreed_jobs": len(agreed),
        "review_disagreements": disagreements,
        "adjudicated_jobs": len(adjudicated),
        "unresolved_jobs": unresolved,
        "metrics_on_agreed_labels": metrics,
        "metric_deltas_percentage_points": {
            "recommend_recall": recommend_delta,
            "stretch_false_reject_rate": stretch_false_reject_delta,
            "false_pass_rate": false_pass_delta,
            "decision_stability": stability_delta,
        },
        "gate_results": gate_results,
        "all_gates_passed": all(value is True for value in gate_results.values()),
        "status": "needs_adjudication" if unresolved else "complete",
    }
