from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import typer
from dotenv import load_dotenv
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from jobradar import __version__, cache
from jobradar.agent import run_search
from jobradar.assessment import TITLE_RELEVANCE_PROMPT_VERSION
from jobradar.cv_extractor import PROMPT_VERSION as CV_PROMPT_VERSION
from jobradar.cv_extractor import extract_cv_profile
from jobradar.cv_reader import read_cv
from jobradar.filters import TITLE_GATE_VERSION
from jobradar.jd_profile import PROMPT_VERSION as JD_PROFILE_PROMPT_VERSION
from jobradar.llm_backend import DEFAULT_MODELS, LLMConfig
from jobradar.matching import PROMPT_VERSION as MATCH_PROMPT_VERSION
from jobradar.paths import COMPARE_RUNS_DIR, REPORTS_DIR
from jobradar.scraping import COARSE_FILTER_VERSION
from jobradar.telemetry import telemetry

app = typer.Typer(no_args_is_help=True, help="Compare baseline flow vs title relevance gate on the same test CV.")

FIXED_TITLES = [
    "AI Engineer",
    "Machine Learning Engineer",
    "LLM Engineer",
    "Software Engineer",
    "Backend Engineer",
]
FIXED_LIMIT_PER_ROLE = 30
FIXED_LINKEDIN_LIMIT_PER_ROLE = 0
FIXED_HOURS_OLD = 168
FIXED_LOCATION = "Ireland"
_TITLE_REJECTION_PREFIX = "Skip (title not relevant): "


def _collect_module_metrics(pipeline_stats) -> dict:
    step_map = {
        "CV 解析": "cv_parse",
        "Title 粗筛": "title_relevance",
        "JD 批量评估": "jd_assessment",
        "JD Profile": "jd_profile",
        "JD CV Matching": "matching",
        "Interview Prep": "interview_prep",
        "Cover Letter": "cover_letter",
        "CV Optimization": "cv_optimization",
    }
    raw = telemetry.summarize_llm_by_step()
    metrics: dict[str, dict] = {}
    for step, data in raw.items():
        key = step_map.get(step, step.lower().replace(" ", "_"))
        metrics[key] = {
            "step": step,
            "calls": int(data.get("calls", 0)),
            "input_tokens": int(data.get("input_tokens", 0)),
            "output_tokens": int(data.get("output_tokens", 0)),
            "elapsed": round(float(data.get("elapsed", 0.0)), 3),
            "provider": data.get("provider", ""),
            "model": data.get("model", ""),
        }

    title_gate = metrics.setdefault(
        "title_relevance",
        {"step": "Title 粗筛", "calls": 0, "input_tokens": 0, "output_tokens": 0, "elapsed": 0.0, "provider": "", "model": ""},
    )
    title_gate["processed"] = int(getattr(pipeline_stats, "title_relevance_in", 0))
    title_gate["rejected"] = int(getattr(pipeline_stats, "title_relevance_rejected", 0))
    title_gate["kept"] = max(0, int(title_gate["processed"]) - int(title_gate["rejected"]))

    jd_assessment = metrics.setdefault(
        "jd_assessment",
        {"step": "JD 批量评估", "calls": 0, "input_tokens": 0, "output_tokens": 0, "elapsed": 0.0, "provider": "", "model": ""},
    )
    jd_assessment["processed"] = int(getattr(pipeline_stats, "llm_assessed", 0))
    jd_assessment["rejected"] = int(getattr(pipeline_stats, "llm_rejected", 0))
    jd_assessment["kept"] = max(0, int(jd_assessment["processed"]) - int(jd_assessment["rejected"]))

    total_in = sum(int(item.get("input_tokens", 0)) for item in metrics.values())
    total_out = sum(int(item.get("output_tokens", 0)) for item in metrics.values())
    total_calls = sum(int(item.get("calls", 0)) for item in metrics.values())
    metrics["_summary"] = {"calls": total_calls, "input_tokens": total_in, "output_tokens": total_out}
    return metrics


def _collect_saved_jobs(dedup_keys: list[str], language: str = "en") -> list[dict]:
    jobs: list[dict] = []
    for key in dedup_keys:
        job = cache.get_job(key, language=language)
        if job is None:
            jobs.append({"dedup_key": key, "title": "", "company": "", "url": ""})
            continue
        jobs.append(
            {
                "dedup_key": key,
                "title": job.title,
                "company": job.company,
                "url": job.url,
            }
        )
    return jobs


def _parse_title_rejections(progress_messages: list[str]) -> list[dict]:
    rejections: list[dict] = []
    for msg in progress_messages:
        if not msg.startswith(_TITLE_REJECTION_PREFIX):
            continue
        body = msg[len(_TITLE_REJECTION_PREFIX):]
        title, sep, reason = body.partition(" — ")
        rejections.append(
            {
                "title": title.strip(),
                "reason": reason.strip() if sep else "",
            }
        )
    return rejections


def _build_diff_summary(baseline_jobs: list[dict], improved_jobs: list[dict]) -> dict:
    baseline_map = {job["dedup_key"]: job for job in baseline_jobs}
    improved_map = {job["dedup_key"]: job for job in improved_jobs}
    overlap = sorted(set(baseline_map) & set(improved_map))
    baseline_only = [baseline_map[key] for key in baseline_map.keys() if key not in improved_map]
    improved_only = [improved_map[key] for key in improved_map.keys() if key not in baseline_map]
    return {
        "overlap_count": len(overlap),
        "baseline_only_count": len(baseline_only),
        "improved_only_count": len(improved_only),
        "baseline_only_jobs": baseline_only,
        "improved_only_jobs": improved_only,
    }


def _run_variant(
    *,
    label: str,
    cv_path: Path,
    provider: str,
    model: str,
    limit_per_role: int,
    linkedin_limit_per_role: int,
    hours_old: int | None,
    gate_enabled: bool,
    db_path: Path | None = None,
) -> dict:
    if db_path is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix=f"jobradar_{label}_")
        db_file = Path(tmp_ctx.__enter__()) / "compare.db"
    else:
        tmp_ctx = None
        db_file = db_path
        db_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        old_db = os.environ.get("CACHE_DB_PATH")
        old_gate = os.environ.get("JOBRADAR_ENABLE_TITLE_RELEVANCE_GATE")
        try:
            os.environ["CACHE_DB_PATH"] = str(db_file)
            os.environ["JOBRADAR_ENABLE_TITLE_RELEVANCE_GATE"] = "1" if gate_enabled else "0"
            telemetry.reset()
            progress_messages: list[str] = []

            cv_text = read_cv(cv_path)
            llm = LLMConfig(provider=provider, model=model)
            profile = extract_cv_profile(cv_text, llm=llm, use_cache=False)
            profile = profile.model_copy(update={"preferred_roles": list(FIXED_TITLES)})
            target_location = FIXED_LOCATION

            started = time.monotonic()
            dedup_keys, pipeline_stats = run_search(
                profile=profile,
                location=target_location,
                llm=llm,
                cv_hash="",
                on_progress=progress_messages.append,
                force_refresh=True,
                language="en",
                limit_per_role=limit_per_role,
                linkedin_limit_per_role=linkedin_limit_per_role,
                hours_old=hours_old,
            )
            elapsed = round(time.monotonic() - started, 1)
            module_metrics = _collect_module_metrics(pipeline_stats)
            total_tokens = int(module_metrics.get("_summary", {}).get("input_tokens", 0)) + int(module_metrics.get("_summary", {}).get("output_tokens", 0))
            saved_jobs = _collect_saved_jobs(dedup_keys, language="en")
            title_gate_rejections = _parse_title_rejections(progress_messages)

            cache.save_search_stats(
                location=target_location,
                roles=profile.preferred_roles,
                provider=provider,
                model=model,
                elapsed=elapsed,
                tokens_in=int(module_metrics.get("_summary", {}).get("input_tokens", 0)),
                tokens_out=int(module_metrics.get("_summary", {}).get("output_tokens", 0)),
                jobs_found=len(dedup_keys),
                run_id=str(getattr(pipeline_stats, "run_id", "")),
                experiment_name=label,
                notes=f"gate_enabled={gate_enabled}",
                scraped_total=pipeline_stats.scraped_total,
                deduped_total=max(0, int(pipeline_stats.prefilter_in) - int(pipeline_stats.skip_dup)),
                filtered_total=len(dedup_keys),
                new_jobs=int(pipeline_stats.new_saved),
                funnel=pipeline_stats.to_dict(),
                app_version=__version__,
                cv_prompt_version=CV_PROMPT_VERSION,
                jd_summary_prompt_version=JD_PROFILE_PROMPT_VERSION,
                match_prompt_version=MATCH_PROMPT_VERSION,
                title_relevance_prompt_version=TITLE_RELEVANCE_PROMPT_VERSION,
                title_gate_version=TITLE_GATE_VERSION,
                coarse_filter_version=COARSE_FILTER_VERSION,
                module_metrics=module_metrics,
            )

            return {
                "label": label,
                "gate_enabled": gate_enabled,
                "db_path": str(db_file),
                "location": target_location,
                "roles": profile.preferred_roles,
                "run_id": str(getattr(pipeline_stats, "run_id", "")),
                "jobs_found": len(dedup_keys),
                "new_jobs": int(pipeline_stats.new_saved),
                "elapsed": elapsed,
                "total_tokens": total_tokens,
                "funnel": pipeline_stats.to_dict(),
                "module_metrics": module_metrics,
                "saved_jobs": saved_jobs,
                "title_gate_rejections": title_gate_rejections,
            }
        finally:
            if old_db is None:
                os.environ.pop("CACHE_DB_PATH", None)
            else:
                os.environ["CACHE_DB_PATH"] = old_db
            if old_gate is None:
                os.environ.pop("JOBRADAR_ENABLE_TITLE_RELEVANCE_GATE", None)
            else:
                os.environ["JOBRADAR_ENABLE_TITLE_RELEVANCE_GATE"] = old_gate
    finally:
        if tmp_ctx is not None:
            tmp_ctx.__exit__(None, None, None)


def _resolve_run_dir(out: Path | None, db_dir: Path | None, keep_db: bool) -> Path | None:
    if db_dir:
        return db_dir
    if keep_db:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return COMPARE_RUNS_DIR / stamp
    if out:
        return out.parent / f"{out.stem}_artifacts"
    return None


@app.command()
def main(
    cv_path: Path = typer.Option(Path("tests/fixtures/test_cv.md"), exists=True, file_okay=True, dir_okay=False),
    provider: str = typer.Option("gemini"),
    model: str = typer.Option("", help="Override model; default uses provider default."),
    keep_db: bool = typer.Option(False, help="Persist baseline/improved sqlite DBs for later inspection."),
    db_dir: Path | None = typer.Option(None, help="Directory to store baseline/improved sqlite DBs."),
    out: Path | None = typer.Option(REPORTS_DIR / "compare_report.json", help="Optional JSON output path."),
) -> None:
    resolved_model = model or DEFAULT_MODELS.get(provider, "")
    run_dir = _resolve_run_dir(out, db_dir, keep_db)
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)

    runs: dict[str, dict] = {}
    variants = [
        ("baseline", "baseline_gate_off", False),
        ("improved", "title_gate_on", True),
    ]
    progress = tqdm(variants, desc="Comparing flows", unit="run")
    for result_key, label, gate_enabled in progress:
        progress.set_postfix(flow=label, refresh=True)
        variant_db_path = None if run_dir is None else run_dir / f"{label}.db"
        runs[result_key] = _run_variant(
            label=label,
            cv_path=cv_path,
            provider=provider,
            model=resolved_model,
            limit_per_role=FIXED_LIMIT_PER_ROLE,
            linkedin_limit_per_role=FIXED_LINKEDIN_LIMIT_PER_ROLE,
            hours_old=FIXED_HOURS_OLD,
            gate_enabled=gate_enabled,
            db_path=variant_db_path,
        )
    baseline = runs["baseline"]
    improved = runs["improved"]
    diff = _build_diff_summary(baseline["saved_jobs"], improved["saved_jobs"])

    def _summary_item(run: dict) -> dict:
        funnel = run["funnel"]
        modules = run["module_metrics"]
        title_gate = modules.get("title_relevance", {})
        jd_assessment = modules.get("jd_assessment", {})
        return {
            "label": run["label"],
            "jobs_found": run["jobs_found"],
            "new_jobs": run["new_jobs"],
            "total_tokens": run["total_tokens"],
            "scraped_total": funnel.get("scraped_total", 0),
            "skip_irrelevant": funnel.get("skip_irrelevant", 0),
            "llm_assessed": funnel.get("llm_assessed", 0),
            "title_relevance_processed": title_gate.get("processed", 0),
            "title_relevance_rejected": title_gate.get("rejected", 0),
            "title_relevance_tokens": int(title_gate.get("input_tokens", 0)) + int(title_gate.get("output_tokens", 0)),
            "jd_assessment_tokens": int(jd_assessment.get("input_tokens", 0)) + int(jd_assessment.get("output_tokens", 0)),
            "tokens_per_new_job": round(run["total_tokens"] / run["new_jobs"], 1) if run["new_jobs"] else None,
        }

    report = {
        "config": {
            "titles": FIXED_TITLES,
            "limit_per_role": FIXED_LIMIT_PER_ROLE,
            "linkedin_limit_per_role": FIXED_LINKEDIN_LIMIT_PER_ROLE,
            "hours_old": FIXED_HOURS_OLD,
            "location": FIXED_LOCATION,
            "source_scope": "indeed_only",
            "db_dir": str(run_dir) if run_dir is not None else "",
        },
        "baseline": baseline,
        "improved": improved,
        "diff": {
            **diff,
            "title_gate_rejections": improved.get("title_gate_rejections", []),
        },
        "summary": {
            "baseline": _summary_item(baseline),
            "improved": _summary_item(improved),
        },
    }

    typer.echo(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if out:
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(f"\nSaved detailed report to {out}")
    if run_dir is not None:
        typer.echo(f"Saved sqlite artifacts to {run_dir}")


if __name__ == "__main__":
    app()
