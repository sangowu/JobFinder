"""Regression tests for newer filtering and assessment helpers."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from jobradar.assessment import (
    _BatchAssessmentResult,
    JDAssessment,
    TitleAssessment,
    _direct_experience_reject,
    batch_assess_jds,
    batch_assess_titles,
)
from jobradar.cv_extractor import extract_cv_profile
from jobradar.jd_profile import _JDProfilePayload, extract_jd_profile
from jobradar.paths import REPORTS_DIR
from jobradar.pipeline_stats import PipelineStats
from jobradar.schemas import CVProfile, JDProfile, JobAssessment, JobResult, MatchScore


@pytest.fixture()
def db(monkeypatch):
    import importlib
    import jobradar.cache as cache_mod

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    monkeypatch.setenv("CACHE_DB_PATH", db_path)
    importlib.reload(cache_mod)
    yield cache_mod


def _make_profile() -> CVProfile:
    return CVProfile(
        summary="Backend and data developer",
        skills=["Python", "SQL", "APIs"],
        years_of_experience=3,
        role_experience_years=[
            {"role": "AI Engineer", "years": 3},
            {"role": "Staff Engineer", "years": 3},
        ],
        seniority="mid",
        preferred_roles=["Backend Engineer", "Data Engineer"],
        preferred_locations=["Dublin"],
    )


def _make_llm():
    from jobradar.llm_backend import LLMConfig

    return LLMConfig(provider="gemini", model="gemini-2.0-flash")


class TestJobAssessmentMatchedKeywords:
    def test_default_empty(self):
        assessment = JobAssessment(score=7, strengths=["a"], weaknesses=["b"])
        assert assessment.matched_keywords == []

    def test_roundtrip_json(self):
        assessment = JobAssessment(
            score=5,
            strengths=["s"],
            weaknesses=["w"],
            matched_keywords=["LLM"],
        )
        restored = JobAssessment.model_validate_json(assessment.model_dump_json())
        assert restored.matched_keywords == ["LLM"]


class TestJDAssessmentConversion:
    def test_to_job_assessment_maps_relevance(self):
        jd_assessment = JDAssessment(
            relevant=False,
            reason="no match",
            score=3,
            strengths=["a"],
            weaknesses=["b"],
            matched_keywords=["X"],
        )

        job_assessment = jd_assessment.to_job_assessment()

        assert isinstance(job_assessment, JobAssessment)
        assert job_assessment.score == 3
        assert job_assessment.matched_keywords == ["X"]
        assert job_assessment.is_relevant is False


class TestWriteCacheBaseModel:
    def test_accepts_job_assessment(self, db):
        from jobradar.tools import write_cache

        assessment = JobAssessment(
            score=9,
            strengths=["x"],
            weaknesses=[],
            matched_keywords=["Go"],
        )
        key = write_cache(
            {
                "title": "Backend Dev",
                "company": "Corp",
                "url": "http://corp.com/1",
                "assessment": assessment,
            }
        )
        result = db.get_job(key)
        assert result is not None
        assert result.assessment is not None
        assert result.assessment.score == 9

    def test_accepts_jd_assessment_basemodel(self, db):
        from jobradar.tools import write_cache

        jd_assessment = JDAssessment(
            relevant=True,
            reason="ok",
            score=7,
            strengths=["s"],
            weaknesses=["w"],
            matched_keywords=["Rust"],
        )
        key = write_cache(
            {
                "title": "Systems Dev",
                "company": "Corp2",
                "url": "http://corp2.com/1",
                "assessment": jd_assessment,
            }
        )
        result = db.get_job(key)
        assert result is not None
        assert result.assessment is not None
        assert result.assessment.score == 7
        assert result.assessment.is_relevant is True


class TestBatchAssessJDs:
    def test_empty_list_returns_empty(self):
        result = batch_assess_jds([], _make_profile(), _make_llm())
        assert result == []

    def test_result_length_matches_input(self):
        jobs = [(f"Job {i}", f"Description {i}") for i in range(11)]
        first_batch = [
            JDAssessment(relevant=True, reason="ok", score=7, strengths=[], weaknesses=[], matched_keywords=[])
            for _ in range(8)
        ]
        second_batch = [
            JDAssessment(relevant=True, reason="ok", score=6, strengths=[], weaknesses=[], matched_keywords=[])
            for _ in range(3)
        ]

        call_count = 0

        def fake_complete(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _BatchAssessmentResult(results=first_batch)
            return _BatchAssessmentResult(results=second_batch)

        with patch("jobradar.assessment.complete_via_tool", side_effect=fake_complete):
            result = batch_assess_jds(jobs, _make_profile(), _make_llm())

        assert len(result) == 11
        assert call_count == 2

    def test_model_short_response_padded(self):
        jobs = [(f"Job {i}", "desc") for i in range(5)]
        short_response = _BatchAssessmentResult(
            results=[
                JDAssessment(
                    relevant=True,
                    reason="ok",
                    score=8,
                    strengths=[],
                    weaknesses=[],
                    matched_keywords=[],
                )
            ]
        )

        with patch("jobradar.assessment.complete_via_tool", return_value=short_response):
            result = batch_assess_jds(jobs, _make_profile(), _make_llm())

        assert len(result) == 5
        assert result[0].score == 8
        for item in result[1:]:
            assert item.relevant is True
            assert item.score == 0

    def test_llm_failure_returns_defaults(self):
        jobs = [("Job A", "desc A"), ("Job B", "desc B")]

        with patch("jobradar.assessment.complete_via_tool", side_effect=Exception("timeout")):
            result = batch_assess_jds(jobs, _make_profile(), _make_llm())

        assert len(result) == 2
        assert all(item.relevant for item in result)
        assert all(item.score == 0 for item in result)

    def test_large_experience_gap_short_circuits_without_llm(self):
        jobs = [("Staff Engineer", "Requires 8+ years of experience in backend systems.")]

        with patch("jobradar.assessment.complete_via_tool") as complete_via_tool:
            result = batch_assess_jds(jobs, _make_profile(), _make_llm())

        complete_via_tool.assert_not_called()
        assert result[0].relevant is False
        assert result[0].score == 0
        assert "8+" in result[0].reason


class TestJDProfileToolCalling:
    def test_extract_jd_profile_uses_tool_wrapper(self, db):
        from jobradar.llm_backend import LLMConfig

        job = JobResult(
            title="AI Engineer",
            company="Acme",
            location="Dublin, Ireland",
            url="https://example.com/job/1",
            description_snippet="We need Python, FastAPI and AWS experience.",
        )
        payload = _JDProfilePayload(
            title="AI Engineer",
            company="Acme",
            location="Dublin, Ireland",
            summary="AI engineering role.",
            required_skills=["Python"],
            preferred_skills=["AWS"],
        )

        with patch("jobradar.jd_profile.complete_via_tool", return_value=payload) as tool_call:
            profile = extract_jd_profile(job, LLMConfig(provider="gemini", model="gemini-2.0-flash"))

        tool_call.assert_called_once()
        assert profile.job_id == job.dedup_key
        assert profile.required_skills == ["Python"]


class TestCVProfileToolCalling:
    def test_extract_cv_profile_uses_tool_wrapper(self, db):
        from jobradar.llm_backend import LLMConfig

        payload = CVProfile(
            summary="AI engineer",
            skills=["Python", "FastAPI"],
            preferred_roles=["AI Engineer"],
            preferred_locations=["Ireland"],
            seniority="new_grad",
        )

        with patch("jobradar.cv_extractor.complete_via_tool", return_value=payload) as tool_call:
            profile = extract_cv_profile(
                "Experienced with Python and FastAPI.",
                llm=LLMConfig(provider="gemini", model="gemini-2.0-flash"),
                use_cache=False,
            )

        tool_call.assert_called_once()
        assert profile.summary == "AI engineer"
        assert profile.skills == ["Python", "FastAPI"]


class TestRuntimeConfig:
    def test_save_env_key_updates_existing_value(self, monkeypatch):
        from jobradar.runtime_config import save_env_key

        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("DEFAULT_PROVIDER=claude\n", encoding="utf-8")

            save_env_key("DEFAULT_PROVIDER", "gemini", env_path=env_path)

            content = env_path.read_text(encoding="utf-8")
            assert "DEFAULT_PROVIDER=gemini" in content
            assert "DEFAULT_PROVIDER=claude" not in content

    def test_get_saved_defaults_uses_env_and_model_defaults(self, monkeypatch):
        from jobradar.runtime_config import get_saved_defaults

        monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
        monkeypatch.delenv("DEFAULT_MODEL", raising=False)
        provider, model = get_saved_defaults()
        assert provider == "claude"
        assert isinstance(model, str)
        assert model


class TestLLMRegistry:
    def test_llm_config_from_defaults_uses_default_model(self):
        from jobradar.llm_backend import DEFAULT_MODELS, LLMConfig

        config = LLMConfig.from_defaults("gemini")

        assert config.provider == "gemini"
        assert config.model == DEFAULT_MODELS["gemini"]


class TestCoverLetterToolCalling:
    def test_generate_cover_letter_uses_tool_wrapper(self, db):
        from jobradar.cover_letter import _CoverLetterPayload, generate_cover_letter
        from jobradar.llm_backend import LLMConfig

        profile = CVProfile(
            summary="AI engineer",
            skills=["Python", "FastAPI"],
            preferred_roles=["AI Engineer"],
            preferred_locations=["Ireland"],
            seniority="new_grad",
        )
        job = JobResult(
            title="AI Engineer",
            company="Acme",
            location="Dublin, Ireland",
            url="https://example.com/job/2",
            description_snippet="Looking for Python and FastAPI experience.",
        )
        jd_profile = JDProfile(
            job_id=job.dedup_key,
            title=job.title,
            company=job.company,
            location=job.location,
        )
        match = MatchScore(
            job_id=job.dedup_key,
            cv_hash="cv-1",
            overall_score=70,
            title_score=90,
            seniority_score=80,
            must_have_score=75,
            nice_to_have_score=60,
            domain_score=80,
            location_score=80,
            language_score=100,
            risk_penalty=5,
            recommendation="apply",
        )
        payload = _CoverLetterPayload(
            subject_line="Application for AI Engineer",
            opener="Dear Hiring Team,",
            body=["Paragraph 1", "Paragraph 2"],
            closing="Best regards,",
            full_text="Dear Hiring Team,\n\nParagraph 1\n\nParagraph 2\n\nBest regards,",
            highlights=["Python", "FastAPI"],
        )

        with patch("jobradar.cover_letter.complete_via_tool", return_value=payload) as tool_call:
            letter = generate_cover_letter(
                profile=profile,
                cv_hash="cv-1",
                job=job,
                jd_profile=jd_profile,
                match=match,
                llm=LLMConfig(provider="gemini", model="gemini-2.0-flash"),
            )

        tool_call.assert_called_once()
        assert letter.job_id == job.dedup_key
        assert letter.subject_line == "Application for AI Engineer"


class TestInterviewPrepToolCalling:
    def test_generate_interview_prep_uses_tool_wrapper(self, db):
        from jobradar.interview_prep import _InterviewPrepPayload, generate_interview_prep
        from jobradar.llm_backend import LLMConfig

        profile = CVProfile(
            summary="AI engineer",
            skills=["Python", "FastAPI"],
            preferred_roles=["AI Engineer"],
            preferred_locations=["Ireland"],
            seniority="new_grad",
        )
        job = JobResult(
            title="AI Engineer",
            company="Acme",
            location="Dublin, Ireland",
            url="https://example.com/job/3",
            description_snippet="Looking for Python and FastAPI experience.",
        )
        jd_profile = JDProfile(
            job_id=job.dedup_key,
            title=job.title,
            company=job.company,
            location=job.location,
        )
        match = MatchScore(
            job_id=job.dedup_key,
            cv_hash="cv-1",
            overall_score=70,
            title_score=90,
            seniority_score=80,
            must_have_score=75,
            nice_to_have_score=60,
            domain_score=80,
            location_score=80,
            language_score=100,
            risk_penalty=5,
            recommendation="apply",
        )
        payload = _InterviewPrepPayload(
            fit_summary="Strong fit for AI engineer role.",
            likely_questions=["Tell me about your Python experience."],
            talking_points=["FastAPI backend experience"],
            stories_to_prepare=["Built an AI workflow end to end"],
            risks_to_address=["Limited enterprise scale exposure"],
            questions_to_ask=["How is success measured in this role?"],
            checklist=["Review JD highlights"],
        )

        with patch("jobradar.interview_prep.complete_via_tool", return_value=payload) as tool_call:
            prep = generate_interview_prep(
                profile=profile,
                cv_hash="cv-1",
                job=job,
                jd_profile=jd_profile,
                match=match,
                llm=LLMConfig(provider="gemini", model="gemini-2.0-flash"),
            )

        tool_call.assert_called_once()
        assert prep.job_id == job.dedup_key
        assert prep.fit_summary == "Strong fit for AI engineer role."


class TestCVOptimizationToolCalling:
    def test_generate_cv_optimization_uses_tool_wrapper(self, db):
        from jobradar.cv_optimization import _CVOptimizationPayload, generate_cv_optimization
        from jobradar.llm_backend import LLMConfig

        profile = CVProfile(
            summary="AI engineer",
            skills=["Python", "FastAPI"],
            preferred_roles=["AI Engineer"],
            preferred_locations=["Ireland"],
            seniority="new_grad",
        )
        job = JobResult(
            title="AI Engineer",
            company="Acme",
            location="Dublin, Ireland",
            url="https://example.com/job/4",
            description_snippet="Looking for Python and FastAPI experience.",
        )
        jd_profile = JDProfile(
            job_id=job.dedup_key,
            title=job.title,
            company=job.company,
            location=job.location,
        )
        match = MatchScore(
            job_id=job.dedup_key,
            cv_hash="cv-1",
            overall_score=70,
            title_score=90,
            seniority_score=80,
            must_have_score=75,
            nice_to_have_score=60,
            domain_score=80,
            location_score=80,
            language_score=100,
            risk_penalty=5,
            recommendation="apply",
        )
        payload = _CVOptimizationPayload(
            summary_strategy="Emphasize backend AI delivery and Python execution.",
            keep_points=["Keep FastAPI backend project."],
            improve_points=["Quantify production impact."],
            bullet_rewrites=["Built Python APIs supporting AI workflow automation."],
            keywords_to_add=["FastAPI", "API design"],
            tailoring_checklist=["Align first bullet with JD requirements."],
        )

        with patch("jobradar.cv_optimization.complete_via_tool", return_value=payload) as tool_call:
            optimization = generate_cv_optimization(
                profile=profile,
                cv_hash="cv-1",
                job=job,
                jd_profile=jd_profile,
                match=match,
                llm=LLMConfig(provider="gemini", model="gemini-2.0-flash"),
            )

        tool_call.assert_called_once()
        assert optimization.job_id == job.dedup_key
        assert optimization.summary_strategy == "Emphasize backend AI delivery and Python execution."


class TestTitleAssessmentHelpers:
    def test_direct_experience_reject_returns_none_when_gap_small(self):
        result = _direct_experience_reject(
            "Backend Engineer",
            "Requires 5+ years of experience.",
            _make_profile(),
            "en",
        )
        assert result is None

    def test_direct_experience_reject_triggers_when_gap_large(self):
        result = _direct_experience_reject(
            "Staff Engineer",
            "Requires 9+ years of experience.",
            _make_profile(),
            "en",
        )
        assert result is not None
        assert result.relevant is False
        assert "9+" in result.reason

    def test_title_overlap_forces_keep(self):
        response = type(
            "Resp",
            (),
            {
                "results": [
                    TitleAssessment(keep=False, reason="model said reject"),
                    TitleAssessment(keep=True, reason="ok"),
                ]
            },
        )()

        with patch("jobradar.assessment.complete_via_tool", return_value=response):
            result = batch_assess_titles(
                ["Backend Engineer", "Finance Manager"],
                _make_profile(),
                _make_llm(),
            )

        assert result[0].keep is True
        assert "保守放行" in result[0].reason
        assert result[1].keep is True


class TestBuildRoleKeywords:
    def test_ai_domain(self):
        from jobradar.agent import _build_role_keywords

        keywords = _build_role_keywords(["Machine Learning Engineer", "AI Researcher"])
        assert "machine" in keywords or "learning" in keywords or "ai" in keywords or "researcher" in keywords

    def test_finance_domain(self):
        from jobradar.agent import _build_role_keywords

        keywords = _build_role_keywords(["Financial Analyst", "Tax Consultant"])
        assert "financial" in keywords
        assert "tax" in keywords
        assert "analyst" not in keywords
        assert "consultant" not in keywords

    def test_accountant_not_filtered(self):
        from jobradar.agent import _build_role_keywords

        keywords = _build_role_keywords(["Accountant", "Auditor"])
        assert "accountant" in keywords
        assert "auditor" in keywords


class TestIsTitleRelevant:
    def test_match(self):
        from jobradar.agent import _is_title_relevant

        assert _is_title_relevant("Financial Analyst", {"financial"})

    def test_no_match(self):
        from jobradar.agent import _is_title_relevant

        assert not _is_title_relevant("Software Engineer", {"financial", "tax"})

    def test_case_insensitive(self):
        from jobradar.agent import _is_title_relevant

        assert _is_title_relevant("SENIOR NURSE MANAGER", {"nurse"})


class TestSeniorityFilter:
    def test_new_grad_blocked_by_senior(self):
        from jobradar.filters import is_seniority_ok

        assert not is_seniority_ok("Senior Software Engineer", "new_grad")

    def test_new_grad_passes_junior(self):
        from jobradar.filters import is_seniority_ok

        assert is_seniority_ok("Graduate Software Engineer", "new_grad")

    def test_mid_blocked_by_staff(self):
        from jobradar.filters import is_seniority_ok

        assert not is_seniority_ok("Staff Engineer", "mid")

    def test_senior_blocked_by_intern(self):
        from jobradar.filters import is_seniority_ok

        assert not is_seniority_ok("Software Engineering Intern", "senior")

    def test_senior_passes_senior(self):
        from jobradar.filters import is_seniority_ok

        assert is_seniority_ok("Senior Software Engineer", "senior")

    def test_sr_dot_filtered(self):
        from jobradar.filters import is_seniority_ok

        assert not is_seniority_ok("Sr. Data Engineer", "new_grad")


class TestJobResultCompatibility:
    def test_job_result_without_match_uses_legacy_assessment(self):
        job = JobResult(
            title="Backend Engineer",
            company="Acme",
            url="https://example.com/job",
            assessment=JobAssessment(
                score=8,
                strengths=["Strong Python background"],
                weaknesses=["Needs more cloud depth"],
                matched_keywords=["Python", "SQL"],
                is_relevant=True,
            ),
        )

        assert job.effective_score == 8.0
        assert job.effective_keywords == ["Python", "SQL"]
        assert job.is_effectively_relevant is True


class TestPipelineStats:
    def test_write_report_accepts_explicit_directory(self, tmp_path: Path):
        stats = PipelineStats(scraped_total=3, saved=1)

        report_path = stats.write_report(directory=tmp_path)

        assert Path(report_path) == tmp_path / "pipeline_stats_latest.json"

    def test_write_report_defaults_to_root_reports_dir(self, monkeypatch: pytest.MonkeyPatch):
        stats = PipelineStats(scraped_total=2, saved=1)
        created_dirs: list[Path] = []

        def fake_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
            created_dirs.append(self)

        def fake_open(
            path: str | Path,
            mode: str = "r",
            encoding: str | None = None,
            *args,
            **kwargs,
        ):
            return io.StringIO()

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        monkeypatch.setattr("builtins.open", fake_open)

        report_path = stats.write_report()

        assert Path(report_path) == REPORTS_DIR / "pipeline_stats_latest.json"
        assert created_dirs == [REPORTS_DIR]
