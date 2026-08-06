"""One-command controlled and optional real-LLM pipeline comparison."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from jobradar.pipeline_benchmark import FrozenDataset, run_paired_benchmark
from jobradar.version_comparison import (
    alternating_order,
    build_version_comparison,
    write_comparison_reports,
)


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.strip()


def _export_profile(repo: Path, output_path: Path, cv_hash: str) -> Path:
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
        "--profile",
        str(profile_path),
        "--location",
        args.location,
        "--output",
        str(dataset_path),
        "--indeed-limit",
        str(args.indeed_limit),
        "--linkedin-limit",
        str(args.linkedin_limit),
        "--hours-old",
        str(args.hours_old),
    ]
    for role in args.roles or []:
        command.extend(["--role", role])
    subprocess.run(command, cwd=repo, check=True)


def _worker_run(
    *,
    worker_script: Path,
    checkout: Path,
    version_mode: str,
    dataset_path: Path,
    profile_path: Path,
    location: str,
    provider: str,
    model: str,
    language: str,
    timing: str,
    speed_factor: float,
    assessment_workers: int,
    runtime_dir: Path,
    run_label: str,
    env: dict[str, str],
) -> dict:
    database_path = runtime_dir / f"{run_label}.db"
    output_path = runtime_dir / f"{run_label}.json"
    command = [
        sys.executable,
        str(worker_script),
        "--checkout",
        str(checkout),
        "--version-mode",
        version_mode,
        "--dataset",
        str(dataset_path),
        "--profile",
        str(profile_path),
        "--database",
        str(database_path),
        "--output",
        str(output_path),
        "--location",
        location,
        "--provider",
        provider,
        "--model",
        model,
        "--language",
        language,
        "--timing",
        timing,
        "--speed-factor",
        str(speed_factor),
        "--assessment-workers",
        str(assessment_workers),
    ]
    completed = _run(command, cwd=checkout, env=env)
    if not output_path.exists():
        raise RuntimeError(f"{version_mode} worker produced no result: {completed.stderr[-1000:]}")
    return json.loads(output_path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare JobRadar serial and streaming pipeline versions")
    parser.add_argument("--baseline", default="09b20c0")
    parser.add_argument("--candidate-checkout", default=".")
    parser.add_argument("--output-dir", default="reports/pipeline_version_comparison")
    parser.add_argument("--profile", help="CVProfile JSON; defaults to the latest cached profile")
    parser.add_argument("--cv-hash", default="")
    parser.add_argument("--dataset", help="Frozen dataset; defaults under output-dir")
    parser.add_argument("--capture", action="store_true", help="Capture dataset with real JobSpy if it does not exist")
    parser.add_argument("--location", default="ireland")
    parser.add_argument("--role", action="append", dest="roles")
    parser.add_argument("--indeed-limit", type=int, default=200)
    parser.add_argument("--linkedin-limit", type=int, default=30)
    parser.add_argument("--hours-old", type=int, default=72)
    parser.add_argument("--controlled-runs", type=int, default=20)
    parser.add_argument("--controlled-warmups", type=int, default=1)
    parser.add_argument("--assessment-delay", type=float, default=0.5)
    parser.add_argument("--timing", choices=("instant", "recorded"), default="recorded")
    parser.add_argument("--speed-factor", type=float, default=1.0)
    parser.add_argument("--assessment-workers", type=int, default=5)
    parser.add_argument("--real-llm", action="store_true", help="Explicitly enable billable real-provider runs")
    parser.add_argument("--real-runs", type=int, default=5)
    parser.add_argument("--real-warmups", type=int, default=0)
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    if not 1 <= args.assessment_workers <= 8:
        parser.error("--assessment-workers must be between 1 and 8")

    repo = Path(args.candidate_checkout).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = output_dir / "inputs"
    profile_path = Path(args.profile).resolve() if args.profile else inputs_dir / "cv-profile.json"
    if not args.profile:
        _export_profile(repo, profile_path, args.cv_hash)
    elif not profile_path.exists():
        parser.error(f"Profile does not exist: {profile_path}")

    dataset_path = Path(args.dataset).resolve() if args.dataset else inputs_dir / "frozen-jobs.json"
    if not dataset_path.exists():
        if not args.capture:
            parser.error(f"Dataset does not exist: {dataset_path}; pass --capture or --dataset")
        _capture_dataset(args, repo, profile_path, dataset_path)

    dataset = FrozenDataset.load(dataset_path)
    controlled_dir = output_dir / "controlled"
    controlled = run_paired_benchmark(
        dataset,
        output_directory=controlled_dir,
        runs=args.controlled_runs,
        warmups=args.controlled_warmups,
        assessment_delay_seconds=args.assessment_delay,
        timing_mode=args.timing,
        speed_factor=args.speed_factor,
    )

    baseline_runs: list[dict] = []
    candidate_runs: list[dict] = []
    real_report: dict | None = None
    if args.real_llm:
        if not args.provider or not args.model:
            parser.error("--provider and --model are required with --real-llm")
        if args.real_runs <= 0 or args.real_warmups < 0:
            parser.error("--real-runs must be positive and --real-warmups must be non-negative")

        load_dotenv(repo / ".env", override=False)
        worker_env = dict(os.environ)
        worker_env["LOG_FILE"] = ""
        worker_script = repo / "scripts" / "pipeline_version_worker.py"
        worktree_root = output_dir / ".worktrees"
        baseline_checkout = worktree_root / f"baseline-{uuid4().hex[:8]}"
        worktree_root.mkdir(parents=True, exist_ok=True)
        _run(["git", "worktree", "add", "--detach", str(baseline_checkout), args.baseline], cwd=repo)
        try:
            candidate_head = _git(repo, "rev-parse", "HEAD")
            if _git(repo, "status", "--porcelain"):
                candidate_head += "+working-tree"
            with tempfile.TemporaryDirectory(prefix="jobradar-version-comparison-", dir=output_dir) as runtime:
                runtime_dir = Path(runtime)
                for pair_index in range(args.real_warmups):
                    for version_mode in alternating_order(pair_index):
                        checkout = baseline_checkout if version_mode == "baseline" else repo
                        _worker_run(
                            worker_script=worker_script,
                            checkout=checkout,
                            version_mode=version_mode,
                            dataset_path=dataset_path,
                            profile_path=profile_path,
                            location=args.location,
                            provider=args.provider,
                            model=args.model,
                            language=args.language,
                            timing=args.timing,
                            speed_factor=args.speed_factor,
                            assessment_workers=args.assessment_workers,
                            runtime_dir=runtime_dir,
                            run_label=f"warmup-{pair_index}-{version_mode}",
                            env=worker_env,
                        )
                for pair_index in range(args.real_runs):
                    pair: dict[str, dict] = {}
                    for version_mode in alternating_order(pair_index):
                        checkout = baseline_checkout if version_mode == "baseline" else repo
                        pair[version_mode] = _worker_run(
                            worker_script=worker_script,
                            checkout=checkout,
                            version_mode=version_mode,
                            dataset_path=dataset_path,
                            profile_path=profile_path,
                            location=args.location,
                            provider=args.provider,
                            model=args.model,
                            language=args.language,
                            timing=args.timing,
                            speed_factor=args.speed_factor,
                            assessment_workers=args.assessment_workers,
                            runtime_dir=runtime_dir,
                            run_label=f"run-{pair_index}-{version_mode}",
                            env=worker_env,
                        )
                    baseline_runs.append(pair["baseline"])
                    candidate_runs.append(pair["candidate"])
            real_report = build_version_comparison(
                baseline_runs,
                candidate_runs,
                baseline_ref=args.baseline,
                candidate_ref=candidate_head,
            )
        finally:
            _run(["git", "worktree", "remove", "--force", str(baseline_checkout)], cwd=repo)
            _run(["git", "worktree", "prune"], cwd=repo)

    json_path, markdown_path = write_comparison_reports(
        output_dir,
        controlled=controlled,
        real=real_report,
        baseline_runs=baseline_runs,
        candidate_runs=candidate_runs,
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    if not controlled["result_equivalent"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
