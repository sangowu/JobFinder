from unittest.mock import patch

from jobradar.matching import (
    _MatchEvidence,
    _deterministic_location_score,
    _overall_score,
    _recommendation,
    adjust_match_for_profile,
    match_job_to_cv,
)
from jobradar.schemas import CVProfile, JDProfile, JobSummary, LanguageProficiency, MatchScore


class DummyEvidence:
    def __init__(self, title, seniority, must_have, nice, domain, location, language, risk):
        self.title_score = title
        self.seniority_score = seniority
        self.must_have_score = must_have
        self.nice_to_have_score = nice
        self.domain_score = domain
        self.location_score = location
        self.language_score = language
        self.risk_penalty = risk


def test_overall_score_programmatic_formula():
    evidence = DummyEvidence(80, 70, 90, 60, 50, 100, 80, 5)
    assert _overall_score(evidence) == 73.2


def test_recommendation_thresholds():
    assert _recommendation(90, []) == "strong_apply"
    assert _recommendation(75, []) == "apply"
    assert _recommendation(62, []) == "stretch_apply"
    assert _recommendation(50, []) == "low_priority"
    assert _recommendation(20, []) == "low_priority"
    assert _recommendation(19.9, []) == "skip"


def test_deterministic_location_score_has_three_levels():
    assert _deterministic_location_score(["Dublin, Ireland"], "Dublin, Ireland") == 100.0
    assert _deterministic_location_score(["Dublin, Ireland"], "Cork, Ireland") == 80.0
    assert _deterministic_location_score(["Dublin, Ireland"], "London, UK") == 30.0


def test_blocking_risk_downgrades():
    assert _recommendation(40, ["Security clearance required"]) == "skip"
    assert _recommendation(80, ["Visa required and unavailable"]) == "apply"
    assert _recommendation(40, ["Onsite required five days a week"]) == "low_priority"


def test_required_language_guard_adjusts_language_score_and_risk():
    profile = CVProfile(
        summary="Graduate software engineer",
        skills=["Python"],
        languages=[LanguageProficiency(name="English", level="C1")],
        seniority="new_grad",
    )
    summary = JobSummary(
        job_id="job-1",
        title="Backend Engineer",
        company="Acme",
        required_languages=[LanguageProficiency(name="German", level="B2")],
    )
    match = MatchScore(
        job_id="job-1",
        cv_hash="cv-1",
        overall_score=70,
        title_score=70,
        seniority_score=80,
        must_have_score=75,
        nice_to_have_score=60,
        domain_score=65,
        location_score=70,
        language_score=90,
        risk_penalty=5,
        recommendation="apply",
        risks=[],
    )
    adjusted = adjust_match_for_profile(profile, summary, match)
    assert adjusted.language_score == 20
    assert adjusted.risk_penalty == 25
    assert any("语言能力" in risk for risk in adjusted.risks)
    assert adjusted.location_score == 30


def test_experience_gap_text_uses_generic_wording():
    profile = CVProfile(
        summary="Graduate software engineer",
        skills=["Python"],
        seniority="junior",
        years_of_experience=1,
    )
    summary = JobSummary(
        job_id="job-2",
        title="Software Engineer",
        company="Cisco",
        years_required=3,
    )
    match = MatchScore(
        job_id="job-2",
        cv_hash="cv-2",
        overall_score=62,
        title_score=80,
        seniority_score=60,
        must_have_score=70,
        nice_to_have_score=60,
        domain_score=70,
        location_score=90,
        language_score=100,
        risk_penalty=15,
        recommendation="stretch_apply",
        matched_keywords=["Python", "API"],
        weaknesses=["工作年限略低于JD要求的3年以上，处于Junior向Mid水平过渡阶段"],
        risks=["级别差距：候选人定位为Junior/New Grad，而JD明确要求Experienced（3+年经验），存在职级不匹配的录取风险"],
    )
    adjusted = adjust_match_for_profile(profile, summary, match, language="zh")
    assert adjusted.weaknesses[0] == "工作年限低于 JD 要求（候选人约 1 年，JD 要求 3+ 年）"
    assert adjusted.risks[0] == "岗位资历要求高于候选人当前工作年限，可能影响面试竞争力"
    assert all("Junior向Mid" not in item for item in adjusted.weaknesses)


def test_office_attendance_risk_is_not_double_penalized():
    profile = CVProfile(
        summary="AI engineer",
        skills=["Python", "PyTorch"],
        seniority="new_grad",
        preferred_locations=["Ireland"],
    )
    summary = JobSummary(
        job_id="job-3",
        title="Graduate Deep Learning Research Engineer",
        company="Valeo",
        location="Tuam, Ireland",
        work_mode="onsite",
    )
    match = MatchScore(
        job_id="job-3",
        cv_hash="cv-3",
        overall_score=48.8,
        title_score=85,
        seniority_score=100,
        must_have_score=90,
        nice_to_have_score=75,
        domain_score=60,
        location_score=100,
        language_score=100,
        risk_penalty=40,
        recommendation="low_priority",
        risks=[
            "12个月固定期限的 Trainee 合同，职业路径存在不确定性",
            "职位要求 Onsite 办公，存在搬迁成本或办公地点限制风险",
        ],
    )

    adjusted = adjust_match_for_profile(profile, summary, match, language="zh")

    assert adjusted.risk_penalty == 40
    assert adjusted.recommendation == "low_priority"
    assert adjusted.location_score == 80


def test_office_attendance_guard_does_not_add_risk_when_absent():
    profile = CVProfile(
        summary="AI engineer",
        skills=["Python"],
        seniority="new_grad",
        preferred_locations=["Ireland"],
    )
    summary = JobSummary(
        job_id="job-4",
        title="AI Engineer",
        company="Acme",
        location="Dublin, Ireland",
        work_mode="onsite",
    )
    match = MatchScore(
        job_id="job-4",
        cv_hash="cv-4",
        overall_score=65,
        title_score=90,
        seniority_score=70,
        must_have_score=70,
        nice_to_have_score=60,
        domain_score=70,
        location_score=90,
        language_score=100,
        risk_penalty=0,
        recommendation="stretch_apply",
        risks=[],
    )

    adjusted = adjust_match_for_profile(profile, summary, match, language="zh")

    assert adjusted.risk_penalty == 10
    assert all("办公室到岗" not in risk for risk in adjusted.risks)
    assert any("搬迁" in risk for risk in adjusted.risks)
    assert adjusted.location_score == 80


def test_match_job_to_cv_uses_tool_wrapper():
    from jobradar.llm_backend import LLMConfig

    profile = CVProfile(
        summary="AI engineer",
        skills=["Python", "FastAPI"],
        preferred_roles=["AI Engineer"],
        preferred_locations=["Ireland"],
        seniority="new_grad",
    )
    jd_profile = JDProfile(
        job_id="job-5",
        title="AI Engineer",
        company="Acme",
        location="Dublin, Ireland",
    )
    evidence = _MatchEvidence(
        title_score=95,
        seniority_score=80,
        must_have_score=80,
        nice_to_have_score=70,
        domain_score=85,
        language_score=100,
        risk_penalty=10,
        title_summary="match",
        seniority_summary="fit",
        must_have_summary="fit",
        nice_to_have_summary="fit",
        domain_summary="fit",
        language_summary="fit",
        risk_summary="minor risk",
    )

    with patch("jobradar.matching.complete_via_tool", return_value=evidence) as tool_call, \
         patch("jobradar.matching.cache.get_job_match", return_value=None), \
         patch("jobradar.matching.cache.save_job_match"):
        result = match_job_to_cv(
            profile=profile,
            jd_profile=jd_profile,
            full_jd="Python, FastAPI, AI systems.",
            llm=LLMConfig(provider="gemini", model="gemini-2.0-flash"),
            language="zh",
        )

    tool_call.assert_called_once()
    assert result.job_id == "job-5"
