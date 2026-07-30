"""测试 Schema 的核心逻辑：归一化和过期判断。"""
import json
from datetime import datetime, timedelta

from jobradar.schemas import (
    CoarseFilterResult,
    CVProfile,
    JobResult,
    SearchSession,
    make_dedup_key,
    normalize_company,
    normalize_title,
)


class TestNormalize:
    def test_company_removes_legal_suffix(self):
        assert normalize_company("Google LLC") == "google"
        assert normalize_company("ByteDance Ltd") == "bytedance"
        assert normalize_company("Stripe Inc.") == "stripe"

    def test_company_lowercase(self):
        assert normalize_company("OpenAI") == "openai"

    def test_title_removes_brackets(self):
        assert normalize_title("Software Engineer (Backend)") == "software engineer"
        assert normalize_title("Senior ML Engineer - London") == "senior ml engineer"

    def test_dedup_key_format(self):
        key = make_dedup_key("Google LLC", "Software Engineer (Backend)")
        assert key == "google|software engineer"

    def test_dedup_key_same_for_variants(self):
        k1 = make_dedup_key("Google LLC", "Software Engineer (Backend)")
        k2 = make_dedup_key("Google", "Software Engineer")
        assert k1 == k2


class TestJobResultExpiry:
    def test_not_expired_within_ttl(self):
        job = JobResult(title="SWE", company="Google", url="http://x.com")
        assert not job.is_expired

    def test_expired_after_ttl(self):
        job = JobResult(
            title="SWE", company="Google", url="http://x.com",
            fetched_at=datetime.utcnow() - timedelta(days=8),
        )
        assert job.is_expired

    def test_expires_at_takes_priority(self):
        job = JobResult(
            title="SWE", company="Google", url="http://x.com",
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        assert job.is_expired

    def test_future_expires_at_not_expired(self):
        job = JobResult(
            title="SWE", company="Google", url="http://x.com",
            expires_at=datetime.utcnow() + timedelta(days=3),
        )
        assert not job.is_expired

    def test_coarse_filter_roundtrip(self):
        job = JobResult(
            title="SWE",
            company="Google",
            url="http://x.com",
            coarse_filter=CoarseFilterResult(
                job_card_id=1,
                keep=True,
                priority="stretch",
                title_match="match",
                location_match="unknown",
                inferred_seniority="senior",
                seniority_confidence="high",
                reason="Senior 但仍可 stretch",
            ),
        )
        assert job.coarse_filter is not None
        assert job.coarse_filter.priority == "stretch"


class TestCVProfileSeniority:
    def test_cv_tool_schema_is_compatible_with_gemini(self):
        schema_text = json.dumps(CVProfile.model_json_schema())

        assert "additionalProperties" not in schema_text

    def test_relevant_years_match_target_role_not_total_experience(self):
        profile = CVProfile(
            summary="Career changer into AI engineering",
            years_of_experience=10,
            preferred_roles=["AI Engineer", "Software Engineer"],
            role_experience_years=[
                {"role": "AI Engineer", "years": 1},
                {"role": "Software Engineer", "years": 2},
            ],
        )

        assert profile.relevant_years_for("Senior AI Engineer") == 1
        assert profile.relevant_years_for("Backend Software Engineer") == 2
        assert profile.relevant_years_for("Retail Manager") is None

    def test_legacy_seniority_backfilled(self):
        profile = CVProfile(
            summary="ML engineer",
            skills=["Python"],
            years_of_experience=2,
            preferred_roles=["ML Engineer"],
            declared_seniority="junior",
            evidence_seniority="mid",
        )
        assert profile.seniority == "mid"
        assert "mid" in profile.eligible_seniority_levels
        assert profile.seniority_display == "junior -> mid"

    def test_defaults_generated_from_new_grad(self):
        profile = CVProfile(
            summary="Graduate developer",
            skills=["Python"],
            years_of_experience=0,
            preferred_roles=["Software Engineer"],
            seniority="new_grad",
        )
        assert "junior" in profile.eligible_seniority_levels
        assert profile.stretch_seniority_levels


class TestSearchSessionExpiry:
    def test_not_expired_within_24h(self):
        s = SearchSession(roles=["SWE"], location="London", seniority="mid", search_language="en")
        s.created_at = datetime.utcnow() - timedelta(hours=23)
        assert not s.is_expired

    def test_expired_after_24h(self):
        s = SearchSession(roles=["SWE"], location="London", seniority="mid", search_language="en")
        s.created_at = datetime.utcnow() - timedelta(hours=25)
        assert s.is_expired

    def test_session_key_consistent(self):
        s1 = SearchSession(roles=["Backend Engineer", "ML Engineer"], location="London", seniority="senior", search_language="en")
        s2 = SearchSession(roles=["ML Engineer", "Backend Engineer"], location="London", seniority="senior", search_language="en")
        # 顺序不同，key 应该相同
        assert s1.session_key == s2.session_key

    def test_session_key_differs_on_location(self):
        s1 = SearchSession(roles=["SWE"], location="London", seniority="mid", search_language="en")
        s2 = SearchSession(roles=["SWE"], location="Berlin", seniority="mid", search_language="en")
        assert s1.session_key != s2.session_key
