"""测试 prefilter 的缓存命中判定：评分按 cv_hash 归属，换 CV 后必须重进管道。"""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest

from jobradar.matching import match_prompt_version
from jobradar.schemas import CVProfile, JobAssessment, JobResult, MatchScore

CURRENT_CV = "cv-current"
OUTDATED_CV = "cv-outdated"


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
    defaults = dict(
        title="Backend Engineer",
        company="Example",
        url="https://example.com/jobs/1",
        description_snippet="Build Python services.",
    )
    defaults.update(kwargs)
    return JobResult(**defaults)


def make_match(job: JobResult, cv_hash: str, recommendation: str = "apply") -> MatchScore:
    return MatchScore(
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
        recommendation=recommendation,
    )


def profile() -> CVProfile:
    return CVProfile(
        summary="Python backend engineer",
        skills=["Python", "SQL"],
        years_of_experience=2,
        seniority="junior",
        preferred_roles=["Backend Engineer"],
        preferred_locations=["Dublin"],
    )


class TestClassifyCacheHit:
    def test_match_under_current_cv_is_reused(self, temp_db):
        from jobradar.search_prefilter import classify_cache_hit

        job = make_job()
        temp_db.save_job(job)
        temp_db.save_job_match(
            make_match(job, CURRENT_CV),
            job.description_snippet,
            prompt_version=match_prompt_version("zh"),
        )

        assert classify_cache_hit(job, CURRENT_CV) == "reuse"

    def test_skip_recommendation_under_current_cv_is_skipped(self, temp_db):
        from jobradar.search_prefilter import classify_cache_hit

        job = make_job()
        temp_db.save_job(job)
        temp_db.save_job_match(
            make_match(job, CURRENT_CV, recommendation="skip"),
            job.description_snippet,
            prompt_version=match_prompt_version("zh"),
        )

        assert classify_cache_hit(job, CURRENT_CV) == "skip"

    def test_match_from_another_cv_triggers_reassessment(self, temp_db):
        """核心：换 CV 后旧匹配结果不算数，该职位必须重进管道。"""
        from jobradar.search_prefilter import classify_cache_hit

        job = make_job()
        temp_db.save_job(job)
        temp_db.save_job_match(
            make_match(job, OUTDATED_CV),
            job.description_snippet,
            prompt_version=match_prompt_version("zh"),
        )

        assert classify_cache_hit(job, CURRENT_CV) == "reassess"

    def test_legacy_rejection_no_longer_blocks_forever(self, temp_db):
        """核心：旧 CV 判定的 is_relevant=False 不再是永久结论。"""
        from jobradar.search_prefilter import classify_cache_hit

        job = make_job(assessment=JobAssessment(score=2, is_relevant=False))
        temp_db.save_job(job)

        assert classify_cache_hit(job, CURRENT_CV) == "reassess"

    def test_stale_prompt_version_triggers_reassessment(self, temp_db):
        from jobradar.search_prefilter import classify_cache_hit

        job = make_job()
        temp_db.save_job(job)
        temp_db.save_job_match(
            make_match(job, CURRENT_CV),
            job.description_snippet,
            prompt_version="match_v1:zh",
        )

        assert classify_cache_hit(job, CURRENT_CV) == "reassess"

    def test_without_cv_hash_falls_back_to_legacy_column(self, temp_db):
        """无从判断 CV 版本时保持既有行为，避免无 CV 场景下丢结果。"""
        from jobradar.search_prefilter import classify_cache_hit

        relevant = make_job(assessment=JobAssessment(score=8, is_relevant=True))
        rejected = make_job(
            company="Other",
            url="https://example.com/jobs/2",
            assessment=JobAssessment(score=1, is_relevant=False),
        )
        unassessed = make_job(company="Third", url="https://example.com/jobs/3")

        assert classify_cache_hit(relevant, "") == "reuse"
        assert classify_cache_hit(rejected, "") == "skip"
        assert classify_cache_hit(unassessed, "") == "reassess"


class TestPrefilterCacheHit:
    def _scraped(self, job: JobResult) -> dict:
        return {
            "title": job.title,
            "company": job.company,
            "location": "Dublin",
            "url": job.url,
            "source": "indeed.ie",
            "description_snippet": job.description_snippet,
            "is_complete": True,
        }

    def test_cached_job_reassessed_when_cv_changed(self, temp_db):
        from jobradar.search_prefilter import prefilter_jobs

        job = make_job()
        temp_db.save_job(job)
        temp_db.save_job_match(
            make_match(job, OUTDATED_CV),
            job.description_snippet,
            prompt_version=match_prompt_version("zh"),
        )

        result = prefilter_jobs(
            [self._scraped(job)],
            set(),
            lambda message: None,
            profile(),
            cv_hash=CURRENT_CV,
        )

        assert result.cache_patch == 1
        assert result.cache_hit == 0
        assert [cached.dedup_key for cached, _ in result.patch_pending] == [job.dedup_key]

    def test_cached_job_reused_when_cv_matches(self, temp_db):
        from jobradar.search_prefilter import prefilter_jobs

        job = make_job()
        temp_db.save_job(job)
        temp_db.save_job_match(
            make_match(job, CURRENT_CV),
            job.description_snippet,
            prompt_version=match_prompt_version("zh"),
        )

        result = prefilter_jobs(
            [self._scraped(job)],
            set(),
            lambda message: None,
            profile(),
            cv_hash=CURRENT_CV,
        )

        assert result.immediate_keys == [job.dedup_key]
        assert result.cache_hit == 1
        assert result.cache_patch == 0

    def test_legacy_rejected_job_reenters_pipeline(self, temp_db):
        """回归：旧 CV 拒绝过的职位曾被 continue 直接丢弃，永不重评。"""
        from jobradar.search_prefilter import prefilter_jobs

        job = make_job(assessment=JobAssessment(score=2, is_relevant=False))
        temp_db.save_job(job)

        result = prefilter_jobs(
            [self._scraped(job)],
            set(),
            lambda message: None,
            profile(),
            cv_hash=CURRENT_CV,
        )

        assert result.cache_patch == 1
        assert result.cache_hit == 0
