from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from jobradar import search_assessment_stage as stage
from jobradar.agent import _resolve_assessment_workers
from jobradar.assessment import JDAssessment
from jobradar.llm_backend import LLMConfig
from jobradar.schemas import CVProfile, JDProfile, JobResult, MatchScore, make_dedup_key
from jobradar.search_prefilter import PrefilterResult


def test_assessment_worker_defaults_and_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ASSESSMENT_WORKERS", raising=False)

    assert _resolve_assessment_workers(None, "gemini") == 5
    assert _resolve_assessment_workers(None, "openai") == 5
    assert _resolve_assessment_workers(None, "ollama") == 1
    assert _resolve_assessment_workers(None, "local") == 1

    monkeypatch.setenv("ASSESSMENT_WORKERS", "3")
    assert _resolve_assessment_workers(None, "gemini") == 3


def _profile() -> CVProfile:
    return CVProfile(
        summary="Python backend engineer",
        skills=["Python", "SQL"],
        years_of_experience=2,
        seniority="junior",
        preferred_roles=["Backend Engineer"],
        preferred_locations=["Dublin"],
    )


def _pending(index: int) -> tuple[dict, str, None]:
    job = {
        "title": "Backend Engineer",
        "company": f"Company {index}",
        "location": "Dublin",
        "url": f"https://example.com/jobs/{index}",
        "source": "indeed.ie",
        "description_snippet": "Build Python and SQL services.",
        "is_complete": True,
    }
    return job, job["description_snippet"], None


def test_three_workers_evaluate_persisted_jobs_concurrently_and_emit_after_commit(
    monkeypatch: pytest.MonkeyPatch,
):
    pending = [_pending(index) for index in range(6)]
    persisted: set[str] = set()
    match_commits: set[str] = set()
    emitted: list[str] = []
    active = 0
    peak = 0
    active_lock = threading.Lock()

    def fake_write_cache(payload: dict) -> str:
        key = make_dedup_key(payload["company"], payload["title"])
        persisted.add(key)
        return key

    def fake_extract(job: JobResult, llm, language="zh", *, persist=True) -> JDProfile:
        nonlocal active, peak
        assert persist is False
        assert job.dedup_key in persisted
        with active_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        return JDProfile(job_id=job.dedup_key, title=job.title, company=job.company)

    def fake_match(profile, jd_profile, full_jd, llm, cv_hash="", language="zh", *, persist=True):
        nonlocal active
        assert persist is False
        time.sleep(0.03)
        with active_lock:
            active -= 1
        return MatchScore(
            job_id=jd_profile.job_id,
            cv_hash=cv_hash,
            overall_score=80,
            title_score=80,
            seniority_score=80,
            must_have_score=80,
            nice_to_have_score=80,
            domain_score=80,
            location_score=100,
            language_score=100,
            risk_penalty=0,
            recommendation="apply",
        )

    monkeypatch.setattr(stage, "write_cache", fake_write_cache)
    monkeypatch.setattr(
        stage,
        "batch_assess_jds",
        lambda jobs, profile, llm, language="zh": [
            JDAssessment(
                relevant=True,
                reason="match",
                score=8,
                strengths=[],
                weaknesses=[],
                matched_keywords=[],
            )
            for _ in jobs
        ],
    )
    monkeypatch.setattr(stage, "extract_jd_profile", fake_extract)
    monkeypatch.setattr(stage, "match_job_to_cv", fake_match)
    monkeypatch.setattr(stage.cache, "save_jd_profile", lambda **kwargs: None)

    def fake_save_match(match_score, **kwargs):
        match_commits.add(match_score.job_id)

    monkeypatch.setattr(stage.cache, "save_job_match", fake_save_match)

    def on_job(key: str) -> None:
        assert key in match_commits
        emitted.append(key)

    metrics = stage.AssessmentConcurrencyMetrics(workers=3)
    with ThreadPoolExecutor(max_workers=3) as executor:
        keys, rejected, saved = stage.flush_assessments(
            PrefilterResult(pending=pending),
            job_all_sources={},
            profile=_profile(),
            llm=LLMConfig(provider="gemini", model="test-model"),
            cv_hash="cv-hash",
            cb=lambda message: None,
            on_job=on_job,
            language="zh",
            evaluation_executor=executor,
            assessment_workers=3,
            concurrency_metrics=metrics,
        )

    assert peak == 3
    assert active == 0
    assert rejected == 0
    assert saved == 6
    assert set(keys) == persisted == match_commits == set(emitted)
    assert metrics.submitted == 6
    assert metrics.completed == 6
    assert metrics.failed == 0
    assert metrics.peak_inflight == 3
