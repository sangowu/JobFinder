"""Run the historical/model/worker benchmark matrix with one command."""
# ruff: noqa: E402, I001
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from jobradar.version_matrix import build_matrix_report, rotated_order, write_matrix_reports


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def _git(repo: Path, *args: str) -> str:
    completed = _run(["git", *args], cwd=repo)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def _run_worker(
    *,
    worker_script: Path,
    checkout: Path,
    version_mode: str,
    assessment_workers: int,
    dataset: Path,
    profile: Path,
    database: Path,
    output: Path,
    provider: str,
    model: str,
    location: str,
    language: str,
    timing: str,
    speed_factor: float,
    env: dict[str, str],
) -> tuple[dict | None, str | None]:
    command = [
        sys.executable,
        str(worker_script),
        "--checkout",
        str(checkout),
        "--version-mode",
        version_mode,
        "--dataset",
        str(dataset),
        "--profile",
        str(profile),
        "--database",
        str(database),
        "--output",
        str(output),
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
    if completed.returncode != 0 or not output.exists():
        detail = completed.stderr.strip() or completed.stdout.strip() or "worker produced no output"
        return None, detail[-4000:]
    return json.loads(output.read_text(encoding="utf-8")), None


def _append_checkpoint(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare historical serial with 3/5 workers across two models")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-dir", default="reports/pipeline_matrix")
    parser.add_argument("--baseline", default="09b20c0")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--reference-model", default="gemini-3.1-flash-lite")
    parser.add_argument("--replacement-model", default="gemini-3.5-flash-lite")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--location", default="ireland")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--timing", choices=("instant", "recorded"), default="recorded")
    parser.add_argument("--speed-factor", type=float, default=1.0)
    args = parser.parse_args()
    if not args.real_llm:
        parser.error("Pass --real-llm to acknowledge provider calls and token usage")
    if args.runs <= 0 or args.warmups < 0 or args.speed_factor <= 0:
        parser.error("runs must be positive; warmups non-negative; speed-factor positive")

    repo = ROOT
    dataset = Path(args.dataset).resolve()
    profile = Path(args.profile).resolve()
    if not dataset.exists() or not profile.exists():
        parser.error("Dataset and profile must both exist")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "pipeline_matrix_runs.jsonl"
    if checkpoint.exists():
        parser.error(f"Checkpoint already exists: {checkpoint}; choose a new --output-dir")

    load_dotenv(repo / ".env", override=False)
    worker_env = dict(os.environ)
    worker_env["LOG_FILE"] = ""
    worker_script = repo / "scripts" / "pipeline_version_worker.py"
    candidate_ref = _git(repo, "rev-parse", "HEAD")
    if _git(repo, "status", "--porcelain"):
        candidate_ref += "+working-tree"

    plans = {
        "reference": {
            "model": args.reference_model,
            "arms": ("baseline", "current-3w", "current-5w"),
        },
        "replacement": {
            "model": args.replacement_model,
            "arms": ("baseline", "current-3w"),
        },
    }
    runs_by_model: dict[str, dict[str, list[dict]]] = {
        model_key: {arm: [] for arm in plan["arms"]} for model_key, plan in plans.items()
    }
    raw_runs: list[dict] = []
    failed_runs: list[dict] = []
    total_measured = args.runs * sum(len(plan["arms"]) for plan in plans.values())
    measured_index = 0

    worktree_root = output_dir / ".worktrees"
    baseline_checkout = worktree_root / f"baseline-{uuid4().hex[:8]}"
    worktree_root.mkdir(parents=True, exist_ok=True)
    added = _run(["git", "worktree", "add", "--detach", str(baseline_checkout), args.baseline], cwd=repo)
    if added.returncode != 0:
        raise RuntimeError(added.stderr.strip() or added.stdout.strip())
    try:
        with tempfile.TemporaryDirectory(prefix="jobradar-pipeline-matrix-", dir=output_dir) as runtime:
            runtime_dir = Path(runtime)
            for block_index in range(args.warmups + args.runs):
                measured = block_index >= args.warmups
                model_order = rotated_order(tuple(plans), block_index)
                for model_key in model_order:
                    plan = plans[model_key]
                    arm_order = rotated_order(plan["arms"], block_index)
                    for arm in arm_order:
                        if measured:
                            measured_index += 1
                        phase = "run" if measured else "warmup"
                        display_index = f"{measured_index}/{total_measured}" if measured else "warmup"
                        print(
                            f"[{display_index}] {phase} block={block_index} model={plan['model']} arm={arm}",
                            flush=True,
                        )
                        version_mode = "baseline" if arm == "baseline" else "candidate"
                        checkout = baseline_checkout if version_mode == "baseline" else repo
                        workers = 1 if arm == "baseline" else int(arm.split("-")[1].removesuffix("w"))
                        label = f"{phase}-{block_index}-{model_key}-{arm}"
                        started = time.monotonic()
                        result, error = _run_worker(
                            worker_script=worker_script,
                            checkout=checkout,
                            version_mode=version_mode,
                            assessment_workers=workers,
                            dataset=dataset,
                            profile=profile,
                            database=runtime_dir / f"{label}.db",
                            output=runtime_dir / f"{label}.json",
                            provider=args.provider,
                            model=plan["model"],
                            location=args.location,
                            language=args.language,
                            timing=args.timing,
                            speed_factor=args.speed_factor,
                            env=worker_env,
                        )
                        wall_elapsed = time.monotonic() - started
                        metadata = {
                            "phase": phase,
                            "block_index": block_index,
                            "model_key": model_key,
                            "model": plan["model"],
                            "arm": arm,
                            "wall_elapsed": wall_elapsed,
                        }
                        if error:
                            failure = {**metadata, "error": error}
                            failed_runs.append(failure)
                            _append_checkpoint(checkpoint, {"status": "failed", **failure})
                            print(f"  failed after {wall_elapsed:.2f}s: {error[-300:]}", flush=True)
                            continue
                        assert result is not None
                        result = {**result, **metadata}
                        _append_checkpoint(checkpoint, {"status": "ok", **result})
                        print(f"  completed in {result['total_elapsed']:.2f}s", flush=True)
                        if measured:
                            runs_by_model[model_key][arm].append(result)
                            raw_runs.append(result)
    finally:
        _run(["git", "worktree", "remove", "--force", str(baseline_checkout)], cwd=repo)
        _run(["git", "worktree", "prune"], cwd=repo)

    missing = {
        f"{model_key}/{arm}": args.runs - len(runs)
        for model_key, arms in runs_by_model.items()
        for arm, runs in arms.items()
        if len(runs) < args.runs
    }
    if missing:
        summary_path = output_dir / "pipeline_matrix_incomplete.json"
        summary_path.write_text(
            json.dumps({"missing_runs": missing, "failed_runs": failed_runs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Matrix incomplete: {summary_path}", flush=True)
        return 2

    report = build_matrix_report(
        runs_by_model,
        model_names={key: str(plan["model"]) for key, plan in plans.items()},
        baseline_ref=args.baseline,
        candidate_ref=candidate_ref,
        failed_runs=failed_runs,
    )
    json_path, markdown_path = write_matrix_reports(output_dir, report, raw_runs)
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
