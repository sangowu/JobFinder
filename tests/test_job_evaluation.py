from __future__ import annotations

from jobradar import job_evaluation
from jobradar import search_assessment_stage as stage
from jobradar.jd_profile import _JDProfilePayload, jd_profile_prompt_version
from jobradar.llm_backend import LLMConfig
from jobradar.matching import _MatchEvidence
from jobradar.schemas import CVProfile, JDProfile, JobResult, MatchScore


def test_uncached_job_profile_and_match_use_one_provider_call(monkeypatch):
    job = JobResult(
        title="Backend Engineer",
        company="Example",
        location="Dublin, Ireland",
        url="https://example.test/job",
        description_snippet="Build Python APIs and SQL services.",
    )
    profile = CVProfile(
        summary="Python backend engineer",
        skills=["Python", "SQL"],
        years_of_experience=2,
        seniority="junior",
        preferred_roles=["Backend Engineer"],
        preferred_locations=["Dublin, Ireland"],
    )
    payload = job_evaluation._CombinedEvaluationPayload(
        jd_profile=_JDProfilePayload(
            title=job.title,
            company=job.company,
            location=job.location,
            required_skills=["Python", "SQL"],
            seniority="junior",
        ),
        match_evidence=_MatchEvidence(
            title_score=95,
            seniority_score=95,
            must_have_score=95,
            nice_to_have_score=80,
            domain_score=90,
            language_score=100,
            risk_penalty=0,
        ),
    )
    calls: list[dict] = []

    def fake_complete_via_tool(**kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(job_evaluation, "complete_via_tool", fake_complete_via_tool)

    jd_profile, match = job_evaluation.evaluate_job_once(
        job,
        profile,
        LLMConfig(provider="gemini", model="test-model"),
        cv_hash="current-cv",
    )

    assert len(calls) == 1
    assert calls[0]["tool_name"] == "evaluate_job_against_cv"
    assert calls[0]["_step"] == "JD Evaluation"
    assert calls[0]["prompt"].count(job.description_snippet) == 1
    assert jd_profile.job_id == job.dedup_key
    assert match.job_id == job.dedup_key
    assert match.cv_hash == "current-cv"
    assert match.recommendation == "strong_apply"


def _task() -> stage.JobEvaluationTask:
    job = JobResult(
        title="Backend Engineer",
        company="Example",
        location="Dublin, Ireland",
        url="https://example.test/job",
        description_snippet="Build Python APIs and SQL services.",
    )
    return stage.JobEvaluationTask(
        key=job.dedup_key,
        job=job,
        full_jd=job.description_snippet,
        kind="fresh",
    )


def _cv_profile() -> CVProfile:
    return CVProfile(
        summary="Python backend engineer",
        skills=["Python", "SQL"],
        years_of_experience=2,
        seniority="junior",
        preferred_roles=["Backend Engineer"],
        preferred_locations=["Dublin, Ireland"],
    )


def _match_score(job_id: str) -> MatchScore:
    return MatchScore(
        job_id=job_id,
        cv_hash="cv-hash",
        overall_score=85,
        title_score=90,
        seniority_score=85,
        must_have_score=85,
        nice_to_have_score=80,
        domain_score=85,
        location_score=100,
        language_score=100,
        risk_penalty=0,
        recommendation="apply",
    )


def test_combined_path_tags_profile_with_its_own_prompt_version(monkeypatch):
    task = _task()
    lookups: list = []

    def fake_get_jd_profile(job_id, description="", prompt_version=""):
        lookups.append(prompt_version)
        return None

    monkeypatch.setattr(stage.cache, "get_jd_profile", fake_get_jd_profile)
    monkeypatch.setattr(
        stage,
        "evaluate_job_once",
        lambda *args, **kwargs: (
            JDProfile(job_id=task.key, title=task.job.title, company=task.job.company),
            _match_score(task.key),
        ),
    )

    outcome = stage._evaluate_job(
        task,
        _cv_profile(),
        LLMConfig(provider="gemini", model="test-model"),
        "cv-hash",
        "zh",
        stage.AssessmentConcurrencyMetrics(workers=1),
    )

    # The lookup accepts either prompt so a combined profile stays reusable.
    assert set(lookups[0]) == {
        jd_profile_prompt_version("zh"),
        job_evaluation.job_evaluation_prompt_version("zh"),
    }
    assert outcome.profile_prompt_version == job_evaluation.job_evaluation_prompt_version("zh")


def test_reused_profile_keeps_the_tag_of_the_prompt_that_produced_it(monkeypatch):
    task = _task()
    cached = JDProfile(job_id=task.key, title=task.job.title, company=task.job.company)
    combined_version = job_evaluation.job_evaluation_prompt_version("zh")

    monkeypatch.setattr(stage.cache, "get_jd_profile", lambda *args, **kwargs: cached)
    monkeypatch.setattr(
        stage.cache, "get_jd_profile_prompt_version", lambda job_id: combined_version
    )
    monkeypatch.setattr(stage, "match_job_to_cv", lambda *args, **kwargs: _match_score(task.key))

    outcome = stage._evaluate_job(
        task,
        _cv_profile(),
        LLMConfig(provider="gemini", model="test-model"),
        "cv-hash",
        "zh",
        stage.AssessmentConcurrencyMetrics(workers=1),
    )

    # Re-committing a reused row must not relabel it as standalone-produced.
    assert outcome.profile_prompt_version == combined_version
