"""Run a repeated, same-code, same-worker Gemini quality comparison."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from jobradar.model_quality_audit import (
    build_quality_report,
    render_quality_markdown,
    write_blind_review_packages,
)


def _run_worker(repo: Path, args: argparse.Namespace, model: str, label: str, runtime: Path, env: dict) -> dict:
    output = runtime / f"{label}.json"
    database = runtime / f"{label}.db"
    command = [
        sys.executable,
        str(repo / "scripts" / "pipeline_version_worker.py"),
        "--checkout", str(repo),
        "--version-mode", "candidate",
        "--dataset", str(Path(args.dataset).resolve()),
        "--profile", str(Path(args.profile).resolve()),
        "--database", str(database),
        "--output", str(output),
        "--location", args.location,
        "--provider", args.provider,
        "--model", model,
        "--language", args.language,
        "--timing", args.timing,
        "--assessment-workers", str(args.workers),
    ]
    completed = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    result = json.loads(output.read_text(encoding="utf-8"))
    result["database_retained"] = bool(args.keep_run_databases)
    result["database_path"] = str(database.resolve()) if args.keep_run_databases else None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Gemini model quality on exactly 100 frozen jobs")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-dir", default="reports/model_quality_standard_100")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--reference-model", default="gemini-3.1-flash-lite")
    parser.add_argument("--replacement-model", default="gemini-3.5-flash-lite")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--location", default="ireland")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--timing", choices=("instant", "recorded"), default="instant")
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument(
        "--keep-run-databases",
        action="store_true",
        help="Retain the isolated SQLite database for every run under OUTPUT_DIR/run_databases",
    )
    args = parser.parse_args()
    if not args.real_llm:
        parser.error("Pass --real-llm to acknowledge provider calls and token usage")
    dataset_payload = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    job_count = len({
        (str(job.get("company") or "").lower(), str(job.get("title") or "").lower())
        for batch in dataset_payload["batches"] for job in batch["jobs"]
    })
    urls = [
        str(job.get("url") or "")
        for batch in dataset_payload["batches"] for job in batch["jobs"]
    ]
    if job_count != 100 or len(set(urls)) != 100 or any(not url for url in urls):
        parser.error(
            f"Quality standard requires 100 unique job keys and URLs; got keys={job_count}, urls={len(set(urls))}"
        )
    if args.runs <= 0 or not 1 <= args.workers <= 8:
        parser.error("runs must be positive and workers must be between 1 and 8")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "model_quality_runs.jsonl"
    if checkpoint.exists():
        parser.error(f"Checkpoint already exists: {checkpoint}; choose a new output directory")
    load_dotenv(ROOT / ".env", override=False)
    env = dict(os.environ)
    env["LOG_FILE"] = ""
    models = {"reference": args.reference_model, "replacement": args.replacement_model}
    runs: dict[str, list[dict]] = {key: [] for key in models}
    total = args.runs * 2
    completed_count = 0
    if args.keep_run_databases:
        persistent_runtime = output_dir / "run_databases"
        if persistent_runtime.exists() and any(persistent_runtime.iterdir()):
            parser.error(f"Run database directory is not empty: {persistent_runtime}")
        persistent_runtime.mkdir(parents=True, exist_ok=True)
        runtime_context = nullcontext(persistent_runtime)
    else:
        runtime_context = tempfile.TemporaryDirectory(prefix="jobradar-quality-audit-", dir=output_dir)

    with runtime_context as runtime_value:
        runtime = Path(runtime_value)
        for run_index in range(args.runs):
            order = tuple(models) if run_index % 2 == 0 else tuple(reversed(models))
            for model_key in order:
                completed_count += 1
                print(f"[{completed_count}/{total}] run={run_index} model={models[model_key]}", flush=True)
                result = _run_worker(
                    ROOT,
                    args,
                    models[model_key],
                    f"run-{run_index}-{model_key}",
                    runtime,
                    env,
                )
                result.update({"model_key": model_key, "quality_run_index": run_index})
                runs[model_key].append(result)
                with checkpoint.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                print(f"  completed in {result['total_elapsed']:.2f}s", flush=True)

    report = build_quality_report(runs, models)
    payload = {**report, "raw_runs": [run for model_runs in runs.values() for run in model_runs]}
    json_path = output_dir / "model_quality_audit.json"
    md_path = output_dir / "model_quality_audit.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_quality_markdown(report), encoding="utf-8")
    reviewer_a, reviewer_b, manifest = write_blind_review_packages(dataset_payload, output_dir / "human_review")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print(f"Reviewer A: {reviewer_a}")
    print(f"Reviewer B: {reviewer_b}")
    print(f"Blind manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
