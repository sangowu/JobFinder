"""A/B the merged JD-evaluation call against a baseline ref, execution mode held constant.

Unlike ``compare_pipeline_versions.py``, both arms run with
``--version-mode candidate``. That script pairs the baseline checkout with
``--version-mode baseline``, which replays the dataset under the pre-streaming
serial contract and forces one assessment worker. That pairing was correct when
the change under test *was* the execution architecture; it is wrong here.

The change under test now lives purely in the code: ``evaluate_job_once`` folds
``JD Profile`` and ``CV Match`` into a single provider call. Both refs already
run the streaming worker pool, so execution mode must stay constant and the
checkout must be the only variable.

Reports two axes:
  - performance: latency, tokens and call counts (``version_comparison``)
  - quality: per-job terminal decisions (``model_quality_audit``)

Real provider calls are mandatory. A merged-call comparison measured against
stubbed assessment latency would be meaningless, so there is no controlled mode.
"""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from jobradar.model_quality_audit import build_quality_report, render_quality_markdown
from jobradar.version_comparison import alternating_order, build_version_comparison

# Both arms use this mode on purpose; see the module docstring.
_EXECUTION_MODE = "candidate"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=True, text=True, capture_output=True)


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.strip()


def _export_profile(output_path: Path, cv_hash: str) -> Path:
    from jobradar import cache

    profile = cache.get_cv_profile(cv_hash) if cv_hash else cache.get_latest_cv_profile()
    if profile is None:
        raise RuntimeError("No cached CVProfile found; pass --profile or parse a CV first")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return output_path


def _capture_dataset(args: argparse.Namespace, repo: Path, profile_path: Path, dataset_path: Path) -> None:
    command = [
        sys.executable,
        str(repo / "scripts" / "capture_pipeline_dataset.py"),
        "--profile", str(profile_path),
        "--location", args.location,
        "--output", str(dataset_path),
        "--indeed-limit", str(args.indeed_limit),
        "--linkedin-limit", str(args.linkedin_limit),
        "--hours-old", str(args.hours_old),
    ]
    for role in args.roles or []:
        command.extend(["--role", role])
    subprocess.run(command, cwd=repo, check=True)


def _worker_run(
    *,
    worker_script: Path,
    checkout: Path,
    dataset_path: Path,
    profile_path: Path,
    args: argparse.Namespace,
    runtime_dir: Path,
    run_label: str,
    env: dict[str, str],
) -> dict:
    output_path = runtime_dir / f"{run_label}.json"
    command = [
        sys.executable,
        str(worker_script),
        "--checkout", str(checkout),
        # Constant across both arms: only --checkout distinguishes them.
        "--version-mode", _EXECUTION_MODE,
        "--dataset", str(dataset_path),
        "--profile", str(profile_path),
        "--database", str(runtime_dir / f"{run_label}.db"),
        "--output", str(output_path),
        "--location", args.location,
        "--provider", args.provider,
        "--model", args.model,
        "--language", args.language,
        "--timing", args.timing,
        "--speed-factor", str(args.speed_factor),
        "--assessment-workers", str(args.assessment_workers),
    ]
    completed = _run(command, cwd=checkout, env=env)
    if not output_path.exists():
        raise RuntimeError(f"{run_label} produced no result: {completed.stderr[-1000:]}")
    return json.loads(output_path.read_text(encoding="utf-8"))


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _delta_pct(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None or baseline == 0:
        return None
    return round((candidate - baseline) / baseline * 100, 2)


def build_call_cost_report(baseline_runs: list[dict], candidate_runs: list[dict]) -> dict:
    """Compare per-run token and call counts.

    ``tokens_out`` is the diagnostic that matters most: merging two calls removes
    a duplicated JD from the prompt (``tokens_in`` should drop) but asks for the
    same fields back. A large ``tokens_out`` drop means the model is emitting
    less than before, which is a quality signal, not a saving.
    """
    report: dict = {}
    for field in ("tokens_in", "tokens_out", "llm_calls"):
        baseline_median = _median([float(run.get(field, 0)) for run in baseline_runs])
        candidate_median = _median([float(run.get(field, 0)) for run in candidate_runs])
        report[field] = {
            "baseline_median": baseline_median,
            "candidate_median": candidate_median,
            "delta_pct": _delta_pct(baseline_median, candidate_median),
        }
    return report


def render_markdown(
    *,
    baseline_ref: str,
    candidate_ref: str,
    performance: dict,
    call_cost: dict,
    quality: dict,
) -> str:
    lines = [
        "# Merged JD-evaluation comparison",
        "",
        f"- Baseline checkout: `{baseline_ref}`",
        f"- Candidate checkout: `{candidate_ref}`",
        f"- Execution mode: `{_EXECUTION_MODE}` on both arms (checkout is the only variable)",
        "",
        "## Call cost",
        "",
        "| Metric | Baseline (median) | Candidate (median) | Delta |",
        "| --- | --- | --- | --- |",
    ]
    for field, values in call_cost.items():
        delta = values["delta_pct"]
        lines.append(
            f"| {field} | {values['baseline_median']} | {values['candidate_median']} | "
            f"{'n/a' if delta is None else f'{delta:+.2f}%'} |"
        )
    lines.extend([
        "",
        "`tokens_out` is expected to stay roughly flat. A large drop means the model "
        "is emitting fewer fields under the merged prompt — treat it as a quality "
        "regression signal, not a saving.",
        "",
        "## Performance",
        "",
        "```json",
        json.dumps(performance.get("improvements", performance), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Quality",
        "",
        "Decisions differ legitimately here: the prompt changed, so per-job diffs are "
        "expected and are not by themselves evidence of regression. Use the blind "
        "review packages and `scripts/score_model_quality_reviews.py` to adjudicate.",
        "",
        render_quality_markdown(quality),
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A/B the merged JD-evaluation call with execution mode held constant",
    )
    parser.add_argument("--baseline", default="e0542ee", help="Ref without the merged evaluation call")
    parser.add_argument("--candidate-checkout", default=".")
    parser.add_argument("--output-dir", default="reports/merged_evaluation_comparison")
    parser.add_argument("--profile", help="CVProfile JSON; defaults to the latest cached profile")
    parser.add_argument("--cv-hash", default="")
    parser.add_argument("--dataset", help="Frozen dataset; defaults under output-dir")
    parser.add_argument("--capture", action="store_true", help="Capture dataset with real JobSpy if missing")
    parser.add_argument("--location", default="ireland")
    parser.add_argument("--role", action="append", dest="roles")
    parser.add_argument("--indeed-limit", type=int, default=200)
    parser.add_argument("--linkedin-limit", type=int, default=30)
    parser.add_argument("--hours-old", type=int, default=72)
    parser.add_argument("--runs", type=int, default=3, help="Paired runs; LLM sampling noise needs >= 3")
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--timing", choices=("instant", "recorded"), default="recorded")
    parser.add_argument("--speed-factor", type=float, default=1.0)
    parser.add_argument("--assessment-workers", type=int, default=5)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--real-llm", action="store_true", help="Explicitly enable billable real-provider runs")
    args = parser.parse_args()

    if not args.real_llm:
        parser.error("This comparison only works against a real provider; pass --real-llm to confirm billing")
    if args.runs <= 0 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups must be non-negative")
    if not 1 <= args.assessment_workers <= 8:
        parser.error("--assessment-workers must be between 1 and 8")

    repo = Path(args.candidate_checkout).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = output_dir / "inputs"

    profile_path = Path(args.profile).resolve() if args.profile else inputs_dir / "cv-profile.json"
    if not args.profile:
        _export_profile(profile_path, args.cv_hash)
    elif not profile_path.exists():
        parser.error(f"Profile does not exist: {profile_path}")

    dataset_path = Path(args.dataset).resolve() if args.dataset else inputs_dir / "frozen-jobs.json"
    if not dataset_path.exists():
        if not args.capture:
            parser.error(f"Dataset does not exist: {dataset_path}; pass --capture or --dataset")
        _capture_dataset(args, repo, profile_path, dataset_path)

    load_dotenv(repo / ".env", override=False)
    worker_env = dict(os.environ)
    worker_env["LOG_FILE"] = ""
    worker_script = repo / "scripts" / "pipeline_version_worker.py"

    worktree_root = output_dir / ".worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)
    baseline_checkout = worktree_root / f"baseline-{uuid4().hex[:8]}"
    _run(["git", "worktree", "add", "--detach", str(baseline_checkout), args.baseline], cwd=repo)

    baseline_runs: list[dict] = []
    candidate_runs: list[dict] = []
    try:
        candidate_ref = _git(repo, "rev-parse", "HEAD")
        if _git(repo, "status", "--porcelain"):
            candidate_ref += "+working-tree"
        checkouts = {"baseline": baseline_checkout, "candidate": repo}

        with tempfile.TemporaryDirectory(prefix="jobradar-merged-eval-", dir=output_dir) as runtime:
            runtime_dir = Path(runtime)
            for pair_index in range(args.warmups):
                for arm in alternating_order(pair_index):
                    _worker_run(
                        worker_script=worker_script,
                        checkout=checkouts[arm],
                        dataset_path=dataset_path,
                        profile_path=profile_path,
                        args=args,
                        runtime_dir=runtime_dir,
                        run_label=f"warmup-{pair_index}-{arm}",
                        env=worker_env,
                    )
            for pair_index in range(args.runs):
                pair: dict[str, dict] = {}
                # Alternate which arm goes first so provider-side drift cancels out.
                for arm in alternating_order(pair_index):
                    pair[arm] = _worker_run(
                        worker_script=worker_script,
                        checkout=checkouts[arm],
                        dataset_path=dataset_path,
                        profile_path=profile_path,
                        args=args,
                        runtime_dir=runtime_dir,
                        run_label=f"run-{pair_index}-{arm}",
                        env=worker_env,
                    )
                baseline_runs.append(pair["baseline"])
                candidate_runs.append(pair["candidate"])
                print(f"pair {pair_index + 1}/{args.runs} done")
    finally:
        _run(["git", "worktree", "remove", "--force", str(baseline_checkout)], cwd=repo)
        _run(["git", "worktree", "prune"], cwd=repo)

    performance = build_version_comparison(
        baseline_runs,
        candidate_runs,
        baseline_ref=args.baseline,
        candidate_ref=candidate_ref,
    )
    call_cost = build_call_cost_report(baseline_runs, candidate_runs)
    quality = build_quality_report(
        {"baseline": baseline_runs, "candidate": candidate_runs},
        {"baseline": args.baseline, "candidate": candidate_ref},
    )

    payload = {
        "schema_version": 1,
        "execution_mode": _EXECUTION_MODE,
        "baseline_ref": args.baseline,
        "candidate_ref": candidate_ref,
        "performance": performance,
        "call_cost": call_cost,
        "quality": quality,
        "baseline_runs": baseline_runs,
        "candidate_runs": candidate_runs,
    }
    json_path = output_dir / "merged_evaluation_comparison.json"
    markdown_path = output_dir / "merged_evaluation_comparison.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(
        render_markdown(
            baseline_ref=args.baseline,
            candidate_ref=candidate_ref,
            performance=performance,
            call_cost=call_cost,
            quality=quality,
        ),
        encoding="utf-8",
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
