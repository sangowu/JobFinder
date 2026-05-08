from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import typer
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobradar.paths import DATA_DIR

load_dotenv(ROOT / ".env")

from jobradar import cache
from jobradar.agent import run_search
from jobradar.cv_extractor import extract_cv_profile
from jobradar.cv_reader import read_cv
from jobradar.llm_backend import DEFAULT_MODELS, LLMConfig

app = typer.Typer(
    no_args_is_help=True,
    help="Run one search and print which titles were rejected, at which stage, and why.",
)


_SKIP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("title_relevance", re.compile(r"^Skip \(title not relevant\):\s*(?P<title>.+?)(?:\s+—\s+(?P<reason>.+))?$")),
    ("coarse_filter", re.compile(r"^Skip \(coarse filter\):\s*(?P<title>.+?)(?:\s+—\s+(?P<reason>.+))?$")),
    ("title_seniority", re.compile(r"^Skip \(title seniority\):\s*(?P<title>.+?)(?:\s+—\s+(?P<reason>.+))?$")),
    ("jd_assessment", re.compile(r"^Skip \(not relevant\):\s*(?P<title>.+?)(?:\s+—\s+(?P<reason>.+))?$")),
    ("final_match", re.compile(r"^Skip \(final match\):\s*(?P<title>.+?)(?:\s+—\s+(?P<reason>.+))?$")),
    ("no_description", re.compile(r"^Skip \(no description\):\s*(?P<title>.+?)(?:\s+—\s+(?P<reason>.+))?$")),
    ("posting_closed", re.compile(r"^Skip \(posting closed\):\s*(?P<title>.+?)(?:\s+—\s+(?P<reason>.+))?$")),
]


def _parse_titles(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_skip_messages(messages: list[str]) -> list[dict]:
    parsed: list[dict] = []
    for message in messages:
        for stage, pattern in _SKIP_PATTERNS:
            match = pattern.match(message)
            if not match:
                continue
            title = (match.group("title") or "").strip()
            reason = (match.groupdict().get("reason") or "").strip()
            parsed.append(
                {
                    "stage": stage,
                    "title": title,
                    "reason": reason,
                    "raw": message,
                }
            )
            break
    return parsed


@app.command()
def main(
    cv_path: Path = typer.Option(Path("tests/fixtures/test_cv.md"), exists=True, file_okay=True, dir_okay=False),
    provider: str = typer.Option("gemini"),
    model: str = typer.Option("", help="Override model; default uses provider default."),
    location: str = typer.Option("", help="Override search location; defaults to CV preferred location."),
    titles: str = typer.Option("", help="Optional comma-separated title override."),
    limit_per_role: int = typer.Option(30),
    linkedin_limit_per_role: int = typer.Option(0),
    hours_old: int = typer.Option(168),
    db_path: Path = typer.Option(DATA_DIR / "jobradar_test_cache.db", help="SQLite cache DB path to use for this inspection run."),
    out: Path | None = typer.Option(None, help="Optional JSON output path."),
) -> None:
    os.environ["CACHE_DB_PATH"] = str(db_path)

    progress_messages: list[str] = []
    cv_text = read_cv(cv_path)
    llm = LLMConfig(provider=provider, model=model or DEFAULT_MODELS.get(provider, ""))
    profile = extract_cv_profile(cv_text, llm=llm, use_cache=False)

    override_titles = _parse_titles(titles)
    if override_titles:
        profile = profile.model_copy(update={"preferred_roles": override_titles})

    target_location = location or (profile.preferred_locations[0] if profile.preferred_locations else "Ireland")

    keys, stats = run_search(
        profile=profile,
        location=target_location,
        llm=llm,
        cv_hash="",
        force_refresh=True,
        language="zh",
        limit_per_role=limit_per_role,
        linkedin_limit_per_role=linkedin_limit_per_role,
        hours_old=hours_old,
        on_progress=progress_messages.append,
    )

    rejected = _parse_skip_messages(progress_messages)
    stage_counts = Counter(item["stage"] for item in rejected)
    visible_jobs: list[dict] = []
    for key in keys:
        job = cache.get_job(key, language="zh")
        if job is None:
            continue
        visible_jobs.append(
            {
                "dedup_key": key,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "effective_score": job.effective_score,
                "recommendation": job.match_score.recommendation if job.match_score else None,
            }
        )

    report = {
        "config": {
            "cv_path": str(cv_path),
            "provider": provider,
            "model": llm.model,
            "location": target_location,
            "titles": profile.preferred_roles,
            "limit_per_role": limit_per_role,
            "linkedin_limit_per_role": linkedin_limit_per_role,
            "hours_old": hours_old,
            "db_path": str(db_path),
        },
        "pipeline_stats": stats.to_dict(),
        "rejected_stage_counts": dict(stage_counts),
        "rejected_items": rejected,
        "visible_jobs": visible_jobs,
    }

    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if out:
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(f"\nSaved rejection report to {out}")


if __name__ == "__main__":
    app()
