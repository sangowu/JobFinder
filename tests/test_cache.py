"""测试缓存层：写入、读取、去重、过期。"""
import importlib
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from jobradar.schemas import (
    CoarseFilterResult,
    CoverLetter,
    CVOptimization,
    InterviewPrep,
    JobResult,
    JobSummary,
    MatchScore,
    SearchSession,
)


# 用临时文件隔离测试数据库
@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    import jobradar.cache as cache_mod

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    monkeypatch.setenv("CACHE_DB_PATH", db_path)
    importlib.reload(cache_mod)
    yield cache_mod

    try:
        os.unlink(db_path)
    except OSError:
        pass


def make_job(**kwargs) -> JobResult:
    defaults = dict(title="Software Engineer", company="Google", url="http://google.com/jobs/1")
    defaults.update(kwargs)
    return JobResult(**defaults)


class TestJobCache:
    def test_insert_and_get(self, temp_db):
        job = make_job(sources=["linkedin.com"])
        temp_db.save_job(job)
        result = temp_db.get_job(job.dedup_key)
        assert result is not None
        assert result.title == "Software Engineer"

    def test_dedup_only_appends_source(self, temp_db):
        job1 = make_job(sources=["linkedin.com"])
        job2 = make_job(sources=["indeed.com"], location="New York")  # 内容不同，应被忽略
        temp_db.save_job(job1)
        temp_db.save_job(job2)

        result = temp_db.get_job(job1.dedup_key)
        assert "linkedin.com" in result.sources
        assert "indeed.com" in result.sources
        assert result.location == ""  # 以第一次为准

    def test_expires_at_updated_on_merge(self, temp_db):
        job1 = make_job(sources=["linkedin.com"])
        future = datetime.utcnow() + timedelta(days=5)
        job2 = make_job(sources=["indeed.com"], expires_at=future)

        temp_db.save_job(job1)
        temp_db.save_job(job2)

        result = temp_db.get_job(job1.dedup_key)
        assert result.expires_at is not None

    def test_expired_job_not_returned(self, temp_db):
        job = make_job(fetched_at=datetime.utcnow() - timedelta(days=8))
        temp_db.save_job(job)
        results = temp_db.get_jobs_by_keys([job.dedup_key])
        assert len(results) == 0

    def test_coarse_filter_roundtrip(self, temp_db):
        job = make_job(
            coarse_filter=CoarseFilterResult(
                job_card_id=3,
                keep=True,
                priority="normal",
                title_match="match",
                location_match="match",
                inferred_seniority="junior",
                seniority_confidence="high",
                reason="职位级别匹配",
            )
        )
        temp_db.save_job(job)
        result = temp_db.get_job(job.dedup_key)
        assert result is not None
        assert result.coarse_filter is not None
        assert result.coarse_filter.priority == "normal"

    def test_search_candidates_persist_before_in_memory_assessment(self, temp_db):
        jobs = [
            {
                "title": "AI Engineer",
                "company": "Example",
                "url": "https://example.com/ai",
                "source": "indeed.ie",
                "description_snippet": "Build Python services.",
            }
        ]

        keys = temp_db.save_search_candidates("run-1", jobs)
        rows = temp_db.get_search_candidates("run-1")

        assert len(keys) == 1
        assert rows[0]["candidate"] == jobs[0]
        assert rows[0]["status"] == "queued"

        temp_db.update_search_candidate_status("run-1", keys, "completed")
        assert temp_db.get_search_candidates("run-1")[0]["status"] == "completed"

    def test_job_summary_roundtrip(self, temp_db):
        job = make_job()
        temp_db.save_job(job)
        summary = JobSummary(
            job_id=job.dedup_key,
            title=job.title,
            company=job.company,
            location="Remote",
            work_mode="remote",
            must_have=["Python"],
            business_overview="Build internal tools",
        )
        temp_db.save_job_summary(job.dedup_key, "desc v1", summary, model_name="gemini/test", prompt_version="v1")

        result = temp_db.get_job(job.dedup_key)
        assert result is not None
        assert result.job_summary is not None
        assert result.job_summary.work_mode == "remote"
        assert result.job_summary.must_have == ["Python"]

    def test_job_summary_invalidated_when_description_changes(self, temp_db):
        job = make_job(description_snippet="desc v1")
        temp_db.save_job(job)
        summary = JobSummary(
            job_id=job.dedup_key,
            title=job.title,
            company=job.company,
            business_overview="Build products",
        )
        temp_db.save_job_summary(job.dedup_key, "desc v1", summary, model_name="gemini/test", prompt_version="v1")

        assert temp_db.get_job_summary(job.dedup_key, "desc v1") is not None
        assert temp_db.get_job_summary(job.dedup_key, "desc v2") is None

    def test_job_match_roundtrip(self, temp_db):
        match = MatchScore(
            job_id="google|software engineer",
            cv_hash="cv123",
            overall_score=82,
            title_score=80,
            seniority_score=70,
            must_have_score=90,
            nice_to_have_score=60,
            domain_score=75,
            location_score=85,
            risk_penalty=3,
            recommendation="apply",
            strengths=["Python fit"],
            weaknesses=["Needs stronger distributed systems"],
            explanation="Strong skill overlap",
        )
        temp_db.save_job_match(match, "desc v1", model_name="gemini/test", prompt_version="v1")

        result = temp_db.get_job_match("google|software engineer", "cv123", "desc v1")
        assert result is not None
        assert result.recommendation == "apply"
        assert result.overall_score == 82

    def test_job_match_invalidated_when_description_changes(self, temp_db):
        match = MatchScore(
            job_id="google|software engineer",
            cv_hash="cv123",
            overall_score=82,
            title_score=80,
            seniority_score=70,
            must_have_score=90,
            nice_to_have_score=60,
            domain_score=75,
            location_score=85,
            risk_penalty=3,
            recommendation="apply",
            explanation="Strong fit",
        )
        temp_db.save_job_match(match, "desc v1")
        assert temp_db.get_job_match("google|software engineer", "cv123", "desc v2") is None

    def test_interview_prep_roundtrip(self, temp_db):
        prep = InterviewPrep(
            job_id="google|software engineer",
            cv_hash="cv123",
            fit_summary="Strong backend alignment",
            likely_questions=["Tell me about a backend system you built."],
            talking_points=["Emphasize Python and APIs"],
            checklist=["Review company mission"],
        )
        temp_db.save_interview_prep(prep, "desc v1", model_name="gemini/test", prompt_version="v1")

        result = temp_db.get_interview_prep("google|software engineer", "cv123", "desc v1")
        assert result is not None
        assert result.fit_summary == "Strong backend alignment"
        assert result.talking_points == ["Emphasize Python and APIs"]

    def test_interview_prep_invalidated_when_description_changes(self, temp_db):
        prep = InterviewPrep(
            job_id="google|software engineer",
            cv_hash="cv123",
            fit_summary="Strong fit",
        )
        temp_db.save_interview_prep(prep, "desc v1")
        assert temp_db.get_interview_prep("google|software engineer", "cv123", "desc v2") is None

    def test_cover_letter_roundtrip(self, temp_db):
        letter = CoverLetter(
            job_id="google|software engineer",
            cv_hash="cv123",
            subject_line="Application for Software Engineer",
            opener="Dear Hiring Team,",
            body=["I am excited to apply."],
            closing="Best regards,",
            full_text="Dear Hiring Team,\n\nI am excited to apply.\n\nBest regards,",
            highlights=["Python", "APIs"],
        )
        temp_db.save_cover_letter(letter, "desc v1", model_name="gemini/test", prompt_version="v1")

        result = temp_db.get_cover_letter("google|software engineer", "cv123", "desc v1")
        assert result is not None
        assert result.subject_line == "Application for Software Engineer"
        assert result.highlights == ["Python", "APIs"]

    def test_cover_letter_invalidated_when_description_changes(self, temp_db):
        letter = CoverLetter(
            job_id="google|software engineer",
            cv_hash="cv123",
            subject_line="Application",
        )
        temp_db.save_cover_letter(letter, "desc v1")
        assert temp_db.get_cover_letter("google|software engineer", "cv123", "desc v2") is None

    def test_cv_optimization_roundtrip(self, temp_db):
        optimization = CVOptimization(
            job_id="google|software engineer",
            cv_hash="cv123",
            summary_strategy="Emphasize backend impact",
            keep_points=["Python APIs"],
            improve_points=["Quantify latency wins"],
            keywords_to_add=["distributed systems"],
        )
        temp_db.save_cv_optimization(optimization, "desc v1", model_name="gemini/test", prompt_version="v1")

        result = temp_db.get_cv_optimization("google|software engineer", "cv123", "desc v1")
        assert result is not None
        assert result.summary_strategy == "Emphasize backend impact"
        assert result.keep_points == ["Python APIs"]

    def test_cv_optimization_invalidated_when_description_changes(self, temp_db):
        optimization = CVOptimization(
            job_id="google|software engineer",
            cv_hash="cv123",
            summary_strategy="Tailor more tightly",
        )
        temp_db.save_cv_optimization(optimization, "desc v1")
        assert temp_db.get_cv_optimization("google|software engineer", "cv123", "desc v2") is None

    def test_get_job_artifacts_aggregates_cached_results(self, temp_db):
        prep = InterviewPrep(job_id="google|software engineer", cv_hash="cv123", fit_summary="Strong fit")
        letter = CoverLetter(job_id="google|software engineer", cv_hash="cv123", subject_line="Application")
        optimization = CVOptimization(job_id="google|software engineer", cv_hash="cv123", summary_strategy="Tailor resume")
        temp_db.save_interview_prep(prep, "desc v1")
        temp_db.save_cover_letter(letter, "desc v1")
        temp_db.save_cv_optimization(optimization, "desc v1")

        artifacts = temp_db.get_job_artifacts("google|software engineer", "cv123", "desc v1")
        assert artifacts["interview_prep"]["exists"] is True
        assert artifacts["cover_letter"]["data"]["subject_line"] == "Application"
        assert artifacts["cv_optimization"]["data"]["summary_strategy"] == "Tailor resume"


class TestSessionCache:
    def test_save_and_get_session(self, temp_db):
        s = SearchSession(roles=["Backend Engineer"], location="London", seniority="senior", search_language="en")
        s.job_dedup_keys = ["google|software engineer"]
        temp_db.save_session(s)

        result = temp_db.get_session(s.session_key)
        assert result is not None
        assert "google|software engineer" in result.job_dedup_keys

    def test_expired_session_returns_none(self, temp_db):
        s = SearchSession(roles=["SWE"], location="London", seniority="mid", search_language="en")
        s.created_at = datetime.utcnow() - timedelta(hours=25)
        temp_db.save_session(s)

        result = temp_db.get_session(s.session_key)
        assert result is None


class TestFailedURLs:
    def test_record_and_check(self, temp_db):
        temp_db.record_failed_url("http://bad.com/job", "login_required")
        assert temp_db.is_failed_url("http://bad.com/job")
        assert not temp_db.is_failed_url("http://good.com/job")

    def test_get_failed_urls_batch(self, temp_db):
        temp_db.record_failed_url("http://a.com", "page_offline")
        temp_db.record_failed_url("http://b.com", "js_rendered")

        failed = temp_db.get_failed_urls(["http://a.com", "http://b.com", "http://c.com"])
        assert "http://a.com" in failed
        assert "http://b.com" in failed
        assert "http://c.com" not in failed


class TestCacheManagement:
    def test_clear_all(self, temp_db):
        temp_db.save_job(make_job())
        temp_db.record_failed_url("http://x.com", "reason")
        temp_db.save_search_candidates(
            "run-clear",
            [{"title": "Engineer", "company": "Example", "url": "https://example.com/job"}],
        )
        temp_db.clear_all()

        assert temp_db.get_job("google|software engineer") is None
        assert not temp_db.is_failed_url("http://x.com")
        assert temp_db.get_search_candidates("run-clear") == []

    def test_clean_expired(self, temp_db):
        fresh_job = make_job(company="Fresh Corp", url="http://fresh.com")
        old_job = make_job(
            company="Old Corp", url="http://old.com",
            fetched_at=datetime.utcnow() - timedelta(days=8),
        )
        temp_db.save_job(fresh_job)
        temp_db.save_job(old_job)

        deleted = temp_db.clean_expired()
        assert deleted >= 1
        assert temp_db.get_job(fresh_job.dedup_key) is not None

    def test_delete_jobs_removes_summary(self, temp_db):
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        summary = JobSummary(job_id=job.dedup_key, title=job.title, company=job.company, business_overview="Build things")
        temp_db.save_job_summary(job.dedup_key, "desc", summary)

        temp_db.delete_jobs([job.dedup_key])
        assert temp_db.get_job_summary(job.dedup_key, "desc") is None

    def test_delete_jobs_removes_match_and_interview_prep(self, temp_db):
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        match = MatchScore(
            job_id=job.dedup_key,
            cv_hash="cv123",
            overall_score=82,
            title_score=80,
            seniority_score=70,
            must_have_score=90,
            nice_to_have_score=60,
            domain_score=75,
            location_score=85,
            risk_penalty=3,
            recommendation="apply",
        )
        prep = InterviewPrep(job_id=job.dedup_key, cv_hash="cv123", fit_summary="Strong fit")
        temp_db.save_job_match(match, "desc")
        temp_db.save_interview_prep(prep, "desc")

        temp_db.delete_jobs([job.dedup_key])
        assert temp_db.get_job_match(job.dedup_key, "cv123", "desc") is None
        assert temp_db.get_interview_prep(job.dedup_key, "cv123", "desc") is None

    def test_delete_jobs_removes_cover_letter(self, temp_db):
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        letter = CoverLetter(job_id=job.dedup_key, cv_hash="cv123", subject_line="Application")
        temp_db.save_cover_letter(letter, "desc")

        temp_db.delete_jobs([job.dedup_key])
        assert temp_db.get_cover_letter(job.dedup_key, "cv123", "desc") is None

    def test_delete_jobs_removes_cv_optimization(self, temp_db):
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        optimization = CVOptimization(job_id=job.dedup_key, cv_hash="cv123", summary_strategy="Tailor harder")
        temp_db.save_cv_optimization(optimization, "desc")

        temp_db.delete_jobs([job.dedup_key])
        assert temp_db.get_cv_optimization(job.dedup_key, "cv123", "desc") is None
