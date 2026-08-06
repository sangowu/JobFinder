"""Compare one-worker and concurrent JobRadar assessment on frozen jobs."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from jobradar.version_comparison import alternating_order, build_version_comparison


def _run_worker(
    *,
    repo: Path,
    dataset: Path,
    profile: Path,
    output: Path,
    database: Path,
    workers: int,
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict:
    command = [
        sys.executable,
        str(repo / "scripts" / "pipeline_version_worker.py"),
        "--checkout",
        str(repo),
        "--version-mode",
        "candidate",
        "--dataset",
        str(dataset),
        "--profile",
        str(profile),
        "--database",
        str(database),
        "--output",
        str(output),
        "--location",
        args.location,
        "--provider",
        args.provider,
        "--model",
        args.model,
        "--language",
        args.language,
        "--timing",
        args.timing,
        "--speed-factor",
        str(args.speed_factor),
        "--assessment-workers",
        str(workers),
    ]
    completed = subprocess.run(
        command,
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Assessment worker run failed ({workers} workers): {detail}")
    return json.loads(output.read_text(encoding="utf-8"))


def _worker_metrics(runs: list[dict]) -> dict[str, float]:
    count = len(runs) or 1
    return {
        "evaluation_peak_inflight_mean": sum(
            run.get("pipeline_stats", {}).get("evaluation_peak_inflight", 0) for run in runs
        ) / count,
        "evaluation_failed_total": sum(
            run.get("pipeline_stats", {}).get("evaluation_failed", 0) for run in runs
        ),
    }


def _render_markdown(report: dict) -> str:
    comparison = report["comparison"]
    one = report["workers_1"]
    candidate_workers = report["candidate_worker_count"]
    candidate = report["workers_candidate"]
    improvement = comparison["improvement_percent"]
    result = comparison["result_comparison"]
    contract = comparison["replay_contract"]
    def _fmt(value: float | None, digits: int = 4) -> str:
        return "n/a" if value is None else f"{value:.{digits}f}"

    return "\n".join(
        [
            "# JobRadar assessment worker comparison",
            "",
            f"- Dataset hash: `{comparison['dataset_hash']}`",
            f"- Profile hash: `{comparison['profile_hash']}`",
            f"- Provider/model: `{comparison['provider']}/{comparison['model']}`",
            f"- Paired runs: `{comparison['baseline']['runs']}`",
            f"- Recorded query events: `{contract['query_event_count']}`; producer elapsed: "
            f"`{contract['producer_finished_offset_seconds']:.4f} s`; producer tail: "
            f"`{contract['producer_tail_seconds']:.4f} s`",
            f"- Mean result-set Jaccard: `{result['jaccard_mean']:.4f}`",
            "",
            f"| Metric | 1 worker | {candidate_workers} workers | Improvement |",
            "|---|---:|---:|---:|",
            f"| Mean total elapsed | {_fmt(comparison['baseline']['total_mean'])} s | "
            f"{_fmt(comparison['candidate']['total_mean'])} s | {_fmt(improvement['total_mean'], 2)}% |",
            f"| P50 total elapsed | {_fmt(comparison['baseline']['total_p50'])} s | "
            f"{_fmt(comparison['candidate']['total_p50'])} s | {_fmt(improvement['total_p50'], 2)}% |",
            f"| P95 total elapsed | {_fmt(comparison['baseline']['total_p95'])} s | "
            f"{_fmt(comparison['candidate']['total_p95'])} s | - |",
            f"| Mean first job | {_fmt(comparison['baseline']['first_job_mean'])} s | "
            f"{_fmt(comparison['candidate']['first_job_mean'])} s | "
            f"{_fmt(improvement['first_job_mean'], 2)}% |",
            f"| Mean LLM calls | {comparison['baseline']['llm_calls_mean']:.1f} | "
            f"{comparison['candidate']['llm_calls_mean']:.1f} | - |",
            f"| Mean input tokens | {comparison['baseline']['tokens_in_mean']:.1f} | "
            f"{comparison['candidate']['tokens_in_mean']:.1f} | - |",
            f"| Mean result count | {comparison['baseline']['result_count_mean']:.1f} | "
            f"{comparison['candidate']['result_count_mean']:.1f} | - |",
            f"| Mean peak evaluation concurrency | {one['evaluation_peak_inflight_mean']:.1f} | "
            f"{candidate['evaluation_peak_inflight_mean']:.1f} | - |",
            f"| Evaluation failures | {one['evaluation_failed_total']:.0f} | "
            f"{candidate['evaluation_failed_total']:.0f} | - |",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare 1-worker and concurrent JobRadar assessment")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-dir", default="reports/assessment_worker_comparison")
    parser.add_argument("--real-llm", action="store_true", help="Explicitly enable real provider calls")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--candidate-workers", type=int, default=5)
    parser.add_argument("--location", default="ireland")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--timing", choices=("instant", "recorded"), default="recorded")
    parser.add_argument("--speed-factor", type=float, default=1.0)
    args = parser.parse_args()
    if not args.real_llm:
        parser.error("Pass --real-llm to acknowledge provider calls and token usage")
    if args.runs <= 0 or args.warmups < 0 or args.speed_factor <= 0:
        parser.error("runs must be positive; warmups non-negative; speed-factor positive")
    if not 2 <= args.candidate_workers <= 8:
        parser.error("--candidate-workers must be between 2 and 8")

    repo = Path(__file__).resolve().parents[1]
    dataset = Path(args.dataset).resolve()
    profile = Path(args.profile).resolve()
    if not dataset.exists() or not profile.exists():
        parser.error("Dataset and profile must both exist")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(repo / ".env", override=False)
    worker_env = dict(os.environ)
    worker_env["LOG_FILE"] = ""

    runs_by_worker: dict[int, list[dict]] = {1: [], args.candidate_workers: []}
    with tempfile.TemporaryDirectory(prefix="jobradar-worker-comparison-", dir=output_dir) as runtime:
        runtime_dir = Path(runtime)
        for pair_index in range(args.warmups + args.runs):
            measured = pair_index >= args.warmups
            order = (
                (1, args.candidate_workers)
                if alternating_order(pair_index) == ("baseline", "candidate")
                else (args.candidate_workers, 1)
            )
            for workers in order:
                label = f"{'run' if measured else 'warmup'}-{pair_index}-{workers}w"
                result = _run_worker(
                    repo=repo,
                    dataset=dataset,
                    profile=profile,
                    output=runtime_dir / f"{label}.json",
                    database=runtime_dir / f"{label}.db",
                    workers=workers,
                    args=args,
                    env=worker_env,
                )
                if measured:
                    runs_by_worker[workers].append(result)

    comparison = build_version_comparison(
        runs_by_worker[1],
        runs_by_worker[args.candidate_workers],
        baseline_ref="assessment-workers=1",
        candidate_ref=f"assessment-workers={args.candidate_workers}",
    )
    report = {
        "schema_version": 1,
        "comparison": comparison,
        "candidate_worker_count": args.candidate_workers,
        "workers_1": _worker_metrics(runs_by_worker[1]),
        "workers_candidate": _worker_metrics(runs_by_worker[args.candidate_workers]),
        "runs_1": runs_by_worker[1],
        "runs_candidate": runs_by_worker[args.candidate_workers],
    }
    json_path = output_dir / "assessment_worker_comparison.json"
    markdown_path = output_dir / "assessment_worker_comparison.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
