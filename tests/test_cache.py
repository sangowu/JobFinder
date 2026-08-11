"""测试缓存层：写入、读取、去重、过期。"""
import importlib
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

from jobradar.schemas import (
    CoarseFilterResult,
    CoverLetter,
    CVOptimization,
    CVProfile,
    InterviewPrep,
    JDProfile,
    JobAssessment,
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

    def test_provider_aliases_do_not_create_duplicate_sources(self, temp_db):
        url = "https://ie.indeed.com/viewjob?jk=123"
        original = make_job(
            url=url,
            sources=["indeed.ie", "linkedin.com"],
            raw_sources=[
                {"source": "indeed.ie", "url": url, "date_posted": "2026-08-07"},
                {
                    "source": "linkedin.com",
                    "url": "https://www.linkedin.com/jobs/view/123",
                    "date_posted": "2026-08-07",
                },
            ],
        )
        alias = make_job(
            url=url,
            sources=["ie.indeed.com"],
            raw_sources=[{"source": "ie.indeed.com", "url": url, "date_posted": ""}],
        )

        temp_db.save_job(original)
        temp_db.save_job(alias)

        result = temp_db.get_job(original.dedup_key)
        assert result is not None
        assert result.sources == ["indeed.ie", "linkedin.com"]
        assert result.raw_sources == original.raw_sources

    def test_read_deduplicates_existing_provider_aliases(self, temp_db):
        url = "https://ie.indeed.com/viewjob?jk=123"
        job = make_job(
            url=url,
            sources=["indeed.ie"],
            raw_sources=[{"source": "indeed.ie", "url": url, "date_posted": "2026-08-07"}],
        )
        temp_db.save_job(job)

        with sqlite3.connect(os.environ["CACHE_DB_PATH"]) as con:
            con.execute(
                "UPDATE job_cache SET sources = ?, raw_sources = ? WHERE dedup_key = ?",
                (
                    '["indeed.ie", "ie.indeed.com", "linkedin.com"]',
                    (
                        '[{"source":"indeed.ie","url":"https://ie.indeed.com/viewjob?jk=123",'
                        '"date_posted":"2026-08-07"},'
                        '{"source":"ie.indeed.com","url":"https://ie.indeed.com/viewjob?jk=123",'
                        '"date_posted":""},'
                        '{"source":"linkedin.com","url":"https://www.linkedin.com/jobs/view/123",'
                        '"date_posted":"2026-08-07"}]'
                    ),
                    job.dedup_key,
                ),
            )

        result = temp_db.get_job(job.dedup_key)
        assert result is not None
        assert result.sources == ["indeed.ie", "linkedin.com"]
        assert result.raw_sources == [
            {"source": "indeed.ie", "url": url, "date_posted": "2026-08-07"},
            {
                "source": "linkedin.com",
                "url": "https://www.linkedin.com/jobs/view/123",
                "date_posted": "2026-08-07",
            },
        ]

    def test_merge_raw_source_treats_provider_alias_as_existing(self, temp_db):
        url = "https://ie.indeed.com/viewjob?jk=123"
        job = make_job(
            url=url,
            sources=["indeed.ie"],
            raw_sources=[{"source": "indeed.ie", "url": url, "date_posted": "2026-08-07"}],
        )
        temp_db.save_job(job)

        temp_db.merge_job_raw_source(
            job.dedup_key,
            {"source": "ie.indeed.com", "url": url, "date_posted": ""},
        )

        result = temp_db.get_job(job.dedup_key)
        assert result is not None
        assert result.sources == ["indeed.ie"]
        assert result.raw_sources == job.raw_sources

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

    def test_job_evaluation_profile_and_match_commit_together(self, temp_db):
        profile = JDProfile(job_id="example|backend engineer", title="Backend Engineer", company="Example")
        match = MatchScore(
            job_id=profile.job_id,
            cv_hash="cv123",
            overall_score=85,
            title_score=90,
            seniority_score=85,
            must_have_score=85,
            nice_to_have_score=80,
            domain_score=85,
            location_score=100,
            language_score=100,
            risk_penalty=0,
            recommendation="strong_apply",
        )

        temp_db.save_job_evaluation(
            profile,
            match,
            "Build Python APIs.",
            profile_prompt_version="profile-v1",
            match_prompt_version="match-v1",
        )

        assert temp_db.get_jd_profile(profile.job_id, "Build Python APIs.", "profile-v1") == profile
        assert temp_db.get_job_match(profile.job_id, "cv123", "Build Python APIs.", "match-v1") == match

    def test_get_jd_profile_accepts_any_of_several_prompt_versions(self, temp_db):
        profile = JDProfile(job_id="example|backend engineer", title="Backend Engineer", company="Example")
        temp_db.save_jd_profile(
            job_id=profile.job_id,
            description="Build Python APIs.",
            profile=profile,
            prompt_version="job_evaluation_v1:zh",
        )

        # A profile written by the combined prompt stays reusable by the match-only path.
        assert temp_db.get_jd_profile(
            profile.job_id,
            "Build Python APIs.",
            ("jd_profile_v1:zh", "job_evaluation_v1:zh"),
        ) == profile
        assert temp_db.get_jd_profile(profile.job_id, "Build Python APIs.", ("jd_profile_v1:zh",)) is None
        assert temp_db.get_jd_profile_prompt_version(profile.job_id) == "job_evaluation_v1:zh"

    def test_job_evaluation_rolls_back_profile_when_match_insert_fails(self, temp_db):
        profile = JDProfile(job_id="example|backend engineer", title="Backend Engineer", company="Example")
        match = MatchScore(
            job_id=profile.job_id,
            cv_hash="cv123",
            overall_score=85,
            title_score=90,
            seniority_score=85,
            must_have_score=85,
            nice_to_have_score=80,
            domain_score=85,
            location_score=100,
            language_score=100,
            risk_penalty=0,
            recommendation="strong_apply",
        )
        with temp_db._conn() as con:
            con.execute(
                """
                CREATE TRIGGER fail_job_match
                BEFORE INSERT ON job_matches
                BEGIN
                  SELECT RAISE(ABORT, 'forced match failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="forced match failure"):
            temp_db.save_job_evaluation(profile, match, "Build Python APIs.")

        assert temp_db.get_jd_profile(profile.job_id, "Build Python APIs.") is None


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


class TestUnassessedJobs:
    """get_unassessed_jobs 只按现代 job_matches 判定，不看 legacy assessment 列。"""

    CV_HASH = "cv-current"

    def _seed_cv(self, temp_db):
        temp_db.save_cv_profile(
            self.CV_HASH,
            CVProfile(summary="Backend engineer", skills=["Python"]),
        )

    def _save_match(self, temp_db, job, cv_hash=None):
        from jobradar.matching import match_prompt_version

        match = MatchScore(
            job_id=job.dedup_key,
            cv_hash=cv_hash or self.CV_HASH,
            overall_score=82,
            title_score=80,
            seniority_score=70,
            must_have_score=90,
            nice_to_have_score=60,
            domain_score=75,
            location_score=85,
            language_score=100,
            risk_penalty=3,
            recommendation="apply",
        )
        temp_db.save_job_match(
            match,
            job.description_snippet,
            prompt_version=match_prompt_version("zh"),
        )

    def test_legacy_assessment_does_not_hide_unmatched_job(self, temp_db):
        """核心修复：旧评分存在不代表当前 CV 评过，该职位仍需重评。"""
        self._seed_cv(temp_db)
        job = make_job(
            description_snippet="Build Python APIs.",
            assessment=JobAssessment(score=7, is_relevant=True),
        )
        temp_db.save_job(job)

        result = temp_db.get_unassessed_jobs()
        assert [j.dedup_key for j in result] == [job.dedup_key]

    def test_rejected_legacy_assessment_still_returned(self, temp_db):
        """旧 CV 判定的 is_relevant=False 不应永久排除该职位。"""
        self._seed_cv(temp_db)
        job = make_job(
            description_snippet="Build Python APIs.",
            assessment=JobAssessment(score=2, is_relevant=False),
        )
        temp_db.save_job(job)

        assert [j.dedup_key for j in temp_db.get_unassessed_jobs()] == [job.dedup_key]

    def test_job_matched_under_current_cv_is_excluded(self, temp_db):
        self._seed_cv(temp_db)
        job = make_job(description_snippet="Build Python APIs.")
        temp_db.save_job(job)
        self._save_match(temp_db, job)

        assert temp_db.get_unassessed_jobs() == []

    def test_match_from_another_cv_does_not_count(self, temp_db):
        """换 CV 后旧 cv_hash 的匹配结果不算已评估。"""
        self._seed_cv(temp_db)
        job = make_job(description_snippet="Build Python APIs.")
        temp_db.save_job(job)
        self._save_match(temp_db, job, cv_hash="cv-outdated")

        assert [j.dedup_key for j in temp_db.get_unassessed_jobs()] == [job.dedup_key]

    def test_each_job_filtered_independently(self, temp_db):
        """回归：过滤条件曾误用循环残留变量，导致整批结果由最后一条决定。"""
        self._seed_cv(temp_db)
        unmatched = [
            make_job(company=f"Company{i}", url=f"http://example.com/{i}", description_snippet="desc")
            for i in range(3)
        ]
        for job in unmatched:
            temp_db.save_job(job)

        # 最后写入的职位已有当前 CV 的匹配结果，不应影响前面三条的判定。
        matched = make_job(company="Matched", url="http://example.com/matched", description_snippet="desc")
        temp_db.save_job(matched)
        self._save_match(temp_db, matched)

        result = {j.dedup_key for j in temp_db.get_unassessed_jobs()}
        assert result == {j.dedup_key for j in unmatched}

    def test_expired_jobs_excluded(self, temp_db):
        self._seed_cv(temp_db)
        expired = make_job(
            company="Expired",
            url="http://example.com/expired",
            description_snippet="desc",
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        live = make_job(company="Live", url="http://example.com/live", description_snippet="desc")
        temp_db.save_job(expired)
        temp_db.save_job(live)

        assert [j.dedup_key for j in temp_db.get_unassessed_jobs()] == [live.dedup_key]

    def test_limit_counts_returned_jobs(self, temp_db):
        """limit 作用于实际返回条数，而非过滤前的扫描量。"""
        self._seed_cv(temp_db)
        for i in range(5):
            temp_db.save_job(
                make_job(company=f"Company{i}", url=f"http://example.com/{i}", description_snippet="desc")
            )

        assert len(temp_db.get_unassessed_jobs(limit=2)) == 2

    def test_assess_cycle_is_idempotent(self, temp_db, monkeypatch):
        """回归：补跑评估必须写入 job_matches，否则同一批职位会被反复捞出。"""
        from jobradar import search_assessment_stage as stage
        from jobradar.llm_backend import LLMConfig

        self._seed_cv(temp_db)
        for i in range(3):
            temp_db.save_job(
                make_job(
                    company=f"Company{i}",
                    url=f"http://example.com/{i}",
                    description_snippet="Build Python APIs.",
                )
            )

        def fake_evaluate(job, profile, llm, cv_hash="", language="zh"):
            jd_profile = JDProfile(job_id=job.dedup_key, title=job.title, company=job.company)
            match = MatchScore(
                job_id=job.dedup_key,
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
            return jd_profile, match

        monkeypatch.setattr(stage, "evaluate_job_once", fake_evaluate)

        pending = temp_db.get_unassessed_jobs()
        assert len(pending) == 3

        succeeded, failed = stage.evaluate_cached_jobs(
            pending,
            profile=CVProfile(summary="Backend engineer", skills=["Python"]),
            llm=LLMConfig(provider="gemini", model="test-model"),
            cv_hash=self.CV_HASH,
        )

        assert (succeeded, failed) == (3, 0)
        # 第二次调用必须为空，否则 assess 会陷入每次重评同一批职位的循环。
        assert temp_db.get_unassessed_jobs() == []


class TestPruneJobMatches:
    def _save(self, temp_db, job_id: str, cv_hash: str, prompt_version: str) -> None:
        match = MatchScore(
            job_id=job_id,
            cv_hash=cv_hash,
            overall_score=70,
            title_score=70,
            seniority_score=70,
            must_have_score=70,
            nice_to_have_score=70,
            domain_score=70,
            location_score=100,
            language_score=100,
            risk_penalty=0,
            recommendation="apply",
        )
        temp_db.save_job_match(match, "desc", prompt_version=prompt_version)

    def _remaining(self, temp_db) -> set[tuple[str, str]]:
        with temp_db._conn() as con:
            return {(r["job_id"], r["prompt_version"]) for r in con.execute("SELECT * FROM job_matches")}

    def test_dry_run_reports_without_deleting(self, temp_db):
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        self._save(temp_db, job.dedup_key, "cv1", "match_v10:zh")

        result = temp_db.prune_job_matches(prompt_version="match_v11:zh", dry_run=True)

        assert result["stale_version"] == 1
        assert result["total"] == 1
        assert result["deleted"] == 0
        assert len(self._remaining(temp_db)) == 1

    def test_deletes_only_stale_prompt_versions(self, temp_db):
        job = make_job(description_snippet="desc")
        other = make_job(company="Other", url="http://example.com/2", description_snippet="desc")
        temp_db.save_job(job)
        temp_db.save_job(other)
        self._save(temp_db, job.dedup_key, "cv1", "match_v10:zh")
        self._save(temp_db, other.dedup_key, "cv1", "match_v11:zh")

        result = temp_db.prune_job_matches(prompt_version="match_v11:zh", dry_run=False)

        assert result["deleted"] == 1
        assert self._remaining(temp_db) == {(other.dedup_key, "match_v11:zh")}

    def test_stale_cv_hash_is_optional(self, temp_db):
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        self._save(temp_db, job.dedup_key, "cv-old", "match_v11:zh")

        untouched = temp_db.prune_job_matches(prompt_version="match_v11:zh", dry_run=True)
        assert untouched["total"] == 0

        targeted = temp_db.prune_job_matches(
            prompt_version="match_v11:zh", keep_cv_hash="cv-current", dry_run=True
        )
        assert targeted["stale_cv"] == 1
        assert targeted["total"] == 1

    def test_orphan_rows_removed_only_when_requested(self, temp_db):
        self._save(temp_db, "ghost|role", "cv1", "match_v11:zh")

        assert temp_db.prune_job_matches(prompt_version="match_v11:zh", dry_run=True)["total"] == 0

        result = temp_db.prune_job_matches(
            prompt_version="match_v11:zh", drop_orphans=True, dry_run=False
        )
        assert result["orphan"] == 1
        assert result["deleted"] == 1
        assert self._remaining(temp_db) == set()

    def test_overlapping_rows_counted_once_in_total(self, temp_db):
        """同一行同时命中多个条件时，total 不应重复计数。"""
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        self._save(temp_db, job.dedup_key, "cv-old", "match_v10:zh")

        result = temp_db.prune_job_matches(
            prompt_version="match_v11:zh", keep_cv_hash="cv-current", dry_run=False
        )

        assert result["stale_version"] == 1
        assert result["stale_cv"] == 1
        assert result["total"] == 1
        assert result["deleted"] == 1

    def test_no_conditions_is_a_noop(self, temp_db):
        job = make_job(description_snippet="desc")
        temp_db.save_job(job)
        self._save(temp_db, job.dedup_key, "cv1", "match_v11:zh")

        result = temp_db.prune_job_matches(dry_run=False)

        assert result["total"] == 0 and result["deleted"] == 0
        assert len(self._remaining(temp_db)) == 1
