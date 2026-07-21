from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator
from jobradar.seniority import (
    LEGACY_SENIORITY_LEVEL,
    default_blocked_levels,
    default_eligible_levels,
    default_stretch_levels,
    infer_title_seniority,
    normalize_seniority,
    normalize_seniority_level,
)

DEFAULT_TTL_DAYS = 7

APPLICATION_STATUSES = (
    "submitted", "assessment", "interview", "offer", "rejected", "withdrawn", "unknown",
)


class ApplicationEmailAnalysis(BaseModel):
    is_job_related: bool = False
    status: Literal[
        "submitted", "assessment", "interview", "offer", "rejected", "withdrawn", "unknown"
    ] = "unknown"
    company: str = ""
    job_title: str = ""
    application_reference: str | None = None
    event_at: datetime | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    summary: str = ""
    classification_reason: str = "unrelated"
    classifier_version: str = "rules-v1"


class ApplicationEvent(BaseModel):
    id: int | None = None
    application_id: int
    email_message_id: str = ""
    gmail_thread_id: str = ""
    event_type: Literal[
        "submitted", "assessment", "interview", "offer", "rejected", "withdrawn", "unknown"
    ]
    event_at: datetime
    confidence: float = Field(default=0, ge=0, le=1)
    summary: str = ""
    created_at: datetime | None = None


class JobApplication(BaseModel):
    id: int | None = None
    job_id: str | None = None
    company: str
    job_title: str
    current_status: Literal[
        "submitted", "assessment", "interview", "offer", "rejected", "withdrawn", "unknown"
    ] = "unknown"
    applied_at: datetime | None = None
    last_event_at: datetime
    source: str = "email"
    external_reference: str | None = None
    gmail_thread_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    events: list[ApplicationEvent] = Field(default_factory=list)

# 检测职位已关闭的关键词模式
_CLOSED_PATTERN = re.compile(
    r"\b("
    r"applications?\s+(are\s+)?(now\s+)?(closed|ended|no longer accepted)"
    r"|no longer (accepting|available|open)"
    r"|position (has been |is )?(filled|closed|removed)"
    r"|this (job|position|vacancy|role) (is|has been|has) (closed|expired|filled|removed)"
    r"|job (is\s+)?no longer available"
    r"|vacancy (is\s+)?(closed|filled)"
    r"|(posting|listing|advert|advertisement)\s+(has\s+)?(expired|been removed)"
    r"|expired on indeed"
    r"|this exact role may not be open"
    r"|posting is to advertise potential job opportunities"
    r")\b",
    re.IGNORECASE,
)

# ─── 公司名/职位名归一化 ───────────────────────────────────────────────────────

_LEGAL_SUFFIXES = re.compile(
    r",?\s*\b(llc|inc|ltd|co|corp|group|gmbh|ag|sa|sas|bv|nv|plc)\.?(?=\s|$)",
    re.IGNORECASE,
)


def normalize_company(name: str) -> str:
    name = _LEGAL_SUFFIXES.sub("", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def normalize_title(title: str) -> str:
    # 去除括号内容及连字符后内容
    title = re.sub(r"\(.*?\)", "", title)
    title = re.sub(r"\s*[-–|].*$", "", title)
    return re.sub(r"\s+", " ", title).strip().lower()


_TITLE_TOKEN_RE = re.compile(r"[a-z0-9+#]+")
_ROLE_VARIANT_MODIFIERS = {
    "applied",
    "python",
    "junior",
    "senior",
    "staff",
    "lead",
    "principal",
    "associate",
    "entry",
    "graduate",
    "new",
    "sr",
    "jr",
}


def _role_tokens(title: str) -> list[str]:
    return _TITLE_TOKEN_RE.findall((title or "").lower())


def _is_role_variant_pair(kept: str, candidate: str) -> bool:
    kept_tokens = _role_tokens(kept)
    candidate_tokens = _role_tokens(candidate)
    if not kept_tokens or not candidate_tokens:
        return False
    if kept_tokens == candidate_tokens:
        return True
    if kept_tokens[-1] != candidate_tokens[-1]:
        return False

    kept_set = set(kept_tokens)
    candidate_set = set(candidate_tokens)
    if not kept_set.issubset(candidate_set):
        return False

    extra = candidate_set - kept_set
    return bool(extra) and extra.issubset(_ROLE_VARIANT_MODIFIERS)


def dedupe_distinct_titles(titles: list[str]) -> list[str]:
    deduped: list[str] = []
    exact_seen: set[str] = set()

    for raw_title in titles:
        title = re.sub(r"\s+", " ", (raw_title or "").strip())
        if not title:
            continue

        normalized = normalize_title(title)
        if normalized in exact_seen:
            continue

        if any(_is_role_variant_pair(kept, title) for kept in deduped):
            continue

        deduped = [kept for kept in deduped if not _is_role_variant_pair(title, kept)]
        deduped.append(title)
        exact_seen = {normalize_title(item) for item in deduped}

    return deduped


def make_dedup_key(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"


def is_closed_posting(text: str) -> bool:
    """判断文本是否包含职位已关闭的信号。"""
    return bool(_CLOSED_PATTERN.search(text))


# ─── CVProfile ────────────────────────────────────────────────────────────────


class CVProfile(BaseModel):
    summary: str = Field(description="一句话专业定位")
    skills: list[str] = Field(default_factory=list)
    languages: list["LanguageProficiency"] = Field(default_factory=list)
    years_of_experience: float | None = Field(default=0, ge=0)
    preferred_locations: list[str] = Field(default_factory=list)
    preferred_roles: list[str] = Field(default_factory=list)
    seniority_raw: list[str] = Field(default_factory=list, description="CV 中直接出现的 seniority 原始表达")
    seniority: LEGACY_SENIORITY_LEVEL = "unknown"
    declared_seniority: str = Field(default="unknown", description="候选人在 CV 中呈现出的资历级别")
    evidence_seniority: str = Field(default="unknown", description="根据经历和职责推断出的资历级别")
    eligible_seniority_levels: list[str] = Field(default_factory=list, description="正常可投递的级别")
    stretch_seniority_levels: list[str] = Field(default_factory=list, description="可尝试但需要提示风险的级别")
    blocked_seniority_levels: list[str] = Field(default_factory=list, description="明显不建议投递的级别")
    seniority_mode: Literal["strict", "balanced", "stretch"] = "balanced"
    reasoning_summary: str = Field(default="", description="模型对资历判断的简短解释")
    search_language: str = Field(
        default="en",
        description="搜索词语言，由目标市场决定，如 en / zh / ja",
    )
    search_terms: list[str] = Field(
        default_factory=list,
        description="基于经验等级和目标市场生成的搜索术语，如 ['graduate programme', 'entry level']",
    )

    @model_validator(mode="after")
    def _populate_seniority_fields(self) -> "CVProfile":
        self.preferred_roles = dedupe_distinct_titles(self.preferred_roles)
        base = self.seniority if self.seniority != "unknown" else "unknown"

        if self.declared_seniority == "unknown":
            self.declared_seniority = base
        if self.evidence_seniority == "unknown":
            self.evidence_seniority = self.declared_seniority

        raw_values = [*self.seniority_raw, self.declared_seniority, self.evidence_seniority, base]
        self.declared_seniority = normalize_seniority(self.seniority_raw, self.declared_seniority)
        self.evidence_seniority = normalize_seniority(raw_values, self.evidence_seniority)
        self.seniority = normalize_seniority(raw_values, self.evidence_seniority or self.declared_seniority or base)

        if not self.eligible_seniority_levels:
            self.eligible_seniority_levels = default_eligible_levels(self.seniority)
        if not self.stretch_seniority_levels:
            self.stretch_seniority_levels = default_stretch_levels(
                self.seniority,
                self.seniority_mode,
            )
        if not self.blocked_seniority_levels:
            self.blocked_seniority_levels = default_blocked_levels(self.seniority)
        return self

    @property
    def effective_seniority(self) -> str:
        return self.evidence_seniority if self.evidence_seniority != "unknown" else self.declared_seniority

    @property
    def seniority_display(self) -> str:
        if self.declared_seniority == self.evidence_seniority:
            return self.effective_seniority
        return f"{self.declared_seniority} -> {self.evidence_seniority}"


# ─── JobResult ────────────────────────────────────────────────────────────────


class JobAssessment(BaseModel):
    score: int = Field(ge=0, le=10, description="CV 与 JD 整体匹配分 0~10")
    strengths: list[str] = Field(default_factory=list, description="CV 相对于该 JD 的优势")
    weaknesses: list[str] = Field(default_factory=list, description="CV 相对于该 JD 的劣势")
    matched_keywords: list[str] = Field(default_factory=list, description="CV 与 JD 重叠的具体技能/关键词")
    is_relevant: bool = Field(default=True, description="LLM 判断是否值得投递，False 表示已拒绝")


class CoarseFilterResult(BaseModel):
    job_card_id: int
    keep: bool = True
    priority: Literal["normal", "stretch", "unknown", "low_priority", "reject"] = "unknown"
    title_match: Literal["match", "partial", "mismatch", "unknown"] = "unknown"
    location_match: Literal["match", "partial", "mismatch", "unknown"] = "unknown"
    inferred_seniority: str = "unknown"
    seniority_confidence: Literal["high", "medium", "low"] = "low"
    reason: str = ""
    reject_reason: str | None = None


_LANGUAGE_CODE_ALIASES = {
    "en": "en",
    "english": "en",
    "英语": "en",
    "inglés": "en",
    "zh": "zh",
    "chinese": "zh",
    "中文": "zh",
    "汉语": "zh",
    "mandarin": "zh",
    "mandarin chinese": "zh",
    "普通话": "zh",
    "yue": "yue",
    "cantonese": "yue",
    "cantonese chinese": "yue",
    "粤语": "yue",
    "es": "es",
    "spanish": "es",
    "español": "es",
    "西班牙语": "es",
    "de": "de",
    "german": "de",
    "deutsch": "de",
    "德语": "de",
    "fr": "fr",
    "french": "fr",
    "français": "fr",
    "法语": "fr",
    "ga": "ga",
    "irish": "ga",
    "gaelic": "ga",
    "爱尔兰语": "ga",
    "ja": "ja",
    "japanese": "ja",
    "日语": "ja",
    "ko": "ko",
    "korean": "ko",
    "韩语": "ko",
    "pt": "pt",
    "portuguese": "pt",
    "葡萄牙语": "pt",
    "it": "it",
    "italian": "it",
    "意大利语": "it",
    "nl": "nl",
    "dutch": "nl",
    "荷兰语": "nl",
    "pl": "pl",
    "polish": "pl",
    "波兰语": "pl",
    "ru": "ru",
    "russian": "ru",
    "俄语": "ru",
    "ar": "ar",
    "arabic": "ar",
    "阿拉伯语": "ar",
    "hi": "hi",
    "hindi": "hi",
    "印地语": "hi",
}


def normalize_language_code(value: str) -> str:
    raw = (value or "").strip().lower()
    return _LANGUAGE_CODE_ALIASES.get(raw, raw)


class LanguageProficiency(BaseModel):
    code: str = ""
    name: str
    level: str = ""

    @model_validator(mode="after")
    def _populate_code(self) -> "LanguageProficiency":
        if self.code:
            self.code = normalize_language_code(self.code)
        elif self.name:
            self.code = normalize_language_code(self.name)
        return self


class JDProfile(BaseModel):
    job_id: str
    title: str
    company: str
    location: str | None = None
    summary: str = ""
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    must_have_requirements: list[str] = Field(default_factory=list)
    education_requirements: list[str] = Field(default_factory=list)
    preferred_roles: list[str] = Field(default_factory=list)
    seniority_raw: list[str] = Field(default_factory=list)
    seniority: str = "unknown"
    years_of_experience: str = ""
    job_type: str | None = None
    work_mode: str | None = None
    title_seniority: str | None = None
    description_seniority: str | None = None
    years_required: int | None = None
    seniority_conflict: bool = False
    seniority_conflict_reason: str | None = None
    must_have: list[str] = Field(default_factory=list)
    good_to_have: list[str] = Field(default_factory=list)
    required_languages: list[LanguageProficiency] = Field(default_factory=list)
    preferred_languages: list[LanguageProficiency] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    business_overview: str = ""
    company_overview: str | None = None
    salary_range: str | None = None
    red_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_seniority_fields(self) -> "JDProfile":
        self.preferred_roles = dedupe_distinct_titles(self.preferred_roles)
        title_level = normalize_seniority_level(self.title_seniority or infer_title_seniority(self.title))
        description_inputs = [*self.seniority_raw, self.description_seniority or "", self.seniority or ""]
        description_level = normalize_seniority(description_inputs, self.description_seniority or self.seniority)
        overall_inputs = [*self.seniority_raw, self.seniority or "", title_level, description_level]

        self.title_seniority = title_level
        self.description_seniority = description_level
        self.seniority = normalize_seniority(overall_inputs, self.seniority or description_level or title_level)

        if title_level != "unknown" and description_level != "unknown" and title_level != description_level:
            self.seniority_conflict = True
            if not self.seniority_conflict_reason:
                self.seniority_conflict_reason = f"title={title_level}, description={description_level}"
        elif self.seniority_conflict and not self.seniority_conflict_reason:
            self.seniority_conflict_reason = None
        return self


class JobSummary(JDProfile):
    """Backward-compatible alias for the previous summary-shaped deep-analysis object."""


class MatchScore(BaseModel):
    job_id: str
    cv_hash: str
    overall_score: float = Field(ge=0, le=100)
    title_score: float = Field(ge=0, le=100)
    seniority_score: float = Field(ge=0, le=100)
    must_have_score: float = Field(ge=0, le=100)
    nice_to_have_score: float = Field(ge=0, le=100)
    domain_score: float = Field(ge=0, le=100)
    location_score: float = Field(ge=0, le=100)
    language_score: float = Field(ge=0, le=100, default=0)
    risk_penalty: float = Field(ge=0, le=100)
    recommendation: Literal["strong_apply", "apply", "stretch_apply", "low_priority", "skip"] = "skip"
    title_summary: str = ""
    seniority_summary: str = ""
    must_have_summary: str = ""
    nice_to_have_summary: str = ""
    domain_summary: str = ""
    location_summary: str = ""
    language_summary: str = ""
    risk_summary: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    missing_must_haves: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    explanation: str = ""


class InterviewPrep(BaseModel):
    job_id: str
    cv_hash: str
    fit_summary: str = ""
    likely_questions: list[str] = Field(default_factory=list)
    talking_points: list[str] = Field(default_factory=list)
    stories_to_prepare: list[str] = Field(default_factory=list)
    risks_to_address: list[str] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)


class CoverLetter(BaseModel):
    job_id: str
    cv_hash: str
    subject_line: str = ""
    opener: str = ""
    body: list[str] = Field(default_factory=list)
    closing: str = ""
    full_text: str = ""
    highlights: list[str] = Field(default_factory=list)


class CVOptimization(BaseModel):
    job_id: str
    cv_hash: str
    summary_strategy: str = ""
    keep_points: list[str] = Field(default_factory=list)
    improve_points: list[str] = Field(default_factory=list)
    bullet_rewrites: list[str] = Field(default_factory=list)
    keywords_to_add: list[str] = Field(default_factory=list)
    tailoring_checklist: list[str] = Field(default_factory=list)


class JobResult(BaseModel):
    title: str
    company: str
    location: str = ""
    url: str
    description_snippet: str = ""
    sources: list[str] = Field(default_factory=list)
    raw_sources: list[dict] = Field(
        default_factory=list,
        description="每个来源的原始记录：[{source, url, date_posted}]",
    )
    date_posted: str = ""          # JobSpy 返回的原始发布日期，如 "2024-04-10"
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    is_complete: bool = True  # False 表示有字段缺失
    coarse_filter: CoarseFilterResult | None = None
    jd_profile: JDProfile | None = None
    job_summary: JobSummary | None = None
    match_score: MatchScore | None = None
    assessment: JobAssessment | None = None

    @model_validator(mode="after")
    def _populate_deep_analysis_fields(self) -> "JobResult":
        if self.jd_profile is None and self.job_summary is not None:
            self.jd_profile = JDProfile.model_validate(self.job_summary.model_dump(mode="json"))
        elif self.job_summary is None and self.jd_profile is not None:
            self.job_summary = JobSummary.model_validate(self.jd_profile.model_dump(mode="json"))
        return self

    @computed_field
    @property
    def dedup_key(self) -> str:
        return make_dedup_key(self.company, self.title)

    @property
    def is_expired(self) -> bool:
        if self.expires_at:
            return datetime.utcnow() > self.expires_at
        age = (datetime.utcnow() - self.fetched_at).days
        return age > DEFAULT_TTL_DAYS

    @property
    def is_possibly_closed(self) -> bool:
        """基于 snippet 关键词判断职位可能已停止招募。"""
        if not self.description_snippet:
            return False
        return bool(_CLOSED_PATTERN.search(self.description_snippet))

    @property
    def effective_score(self) -> float | None:
        if self.match_score is not None:
            return self.match_score.overall_score
        if self.assessment is not None:
            return float(self.assessment.score)
        return None

    @property
    def effective_strengths(self) -> list[str]:
        if self.match_score is not None:
            return self.match_score.strengths
        if self.assessment is not None:
            return self.assessment.strengths
        return []

    @property
    def effective_weaknesses(self) -> list[str]:
        if self.match_score is not None:
            return self.match_score.weaknesses
        if self.assessment is not None:
            return self.assessment.weaknesses
        return []

    @property
    def effective_keywords(self) -> list[str]:
        if self.match_score is not None and (self.jd_profile is not None or self.job_summary is not None):
            return self.match_score.matched_keywords[:6]
        if self.assessment is not None:
            return self.assessment.matched_keywords[:6]
        return []

    @property
    def is_effectively_relevant(self) -> bool:
        if self.match_score is not None:
            return self.match_score.recommendation != "skip"
        if self.assessment is not None:
            return self.assessment.is_relevant
        return True


# ─── SearchSession ────────────────────────────────────────────────────────────


class SearchSession(BaseModel):
    roles: list[str]
    location: str
    seniority: str
    search_language: str
    sources: list[str] = Field(default_factory=lambda: ["indeed"])
    job_dedup_keys: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @computed_field
    @property
    def session_key(self) -> str:
        data = {
            "roles": sorted(self.roles),
            "location": self.location.lower().strip(),
            "seniority": self.seniority,
            "sources": sorted(self.sources),
        }
        return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @property
    def is_expired(self) -> bool:
        ttl_hours = int(os.getenv("SESSION_TTL_HOURS", 24))
        age_hours = (datetime.utcnow() - self.created_at).total_seconds() / 3600
        return age_hours > ttl_hours


# ─── FailedURL ────────────────────────────────────────────────────────────────


class FailedURL(BaseModel):
    url: str
    reason: str
    skipped_at: datetime = Field(default_factory=datetime.utcnow)


# ─── SearchQuery（内部构造，用于日志和测试）───────────────────────────────────


class SearchQuery(BaseModel):
    keywords: str
    location: str
    role: str
    language: str = "en"
    max_results: int = 10
