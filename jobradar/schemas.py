from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

DEFAULT_TTL_DAYS = 7
LEGACY_SENIORITY_LEVEL = Literal["intern", "new_grad", "junior", "mid", "senior", "lead", "unknown"]

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


def make_dedup_key(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"


def is_closed_posting(text: str) -> bool:
    """判断文本是否包含职位已关闭的信号。"""
    return bool(_CLOSED_PATTERN.search(text))


# ─── CVProfile ────────────────────────────────────────────────────────────────


class CVProfile(BaseModel):
    summary: str = Field(description="一句话专业定位")
    skills: list[str] = Field(default_factory=list)
    years_of_experience: float | None = Field(default=0, ge=0)
    preferred_locations: list[str] = Field(default_factory=list)
    preferred_roles: list[str] = Field(default_factory=list)
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
        base = self.seniority if self.seniority != "unknown" else "unknown"

        if self.declared_seniority == "unknown":
            self.declared_seniority = base
        if self.evidence_seniority == "unknown":
            self.evidence_seniority = self.declared_seniority

        effective = self.evidence_seniority
        if effective == "unknown":
            effective = self.declared_seniority
        if effective == "unknown":
            effective = base
        self.seniority = _to_legacy_seniority(effective)

        if not self.eligible_seniority_levels:
            self.eligible_seniority_levels = _default_eligible_levels(self.seniority)
        if not self.stretch_seniority_levels:
            self.stretch_seniority_levels = _default_stretch_levels(
                self.seniority,
                self.seniority_mode,
            )
        if not self.blocked_seniority_levels:
            self.blocked_seniority_levels = _default_blocked_levels(self.seniority)
        return self

    @property
    def effective_seniority(self) -> str:
        return self.evidence_seniority if self.evidence_seniority != "unknown" else self.declared_seniority

    @property
    def seniority_display(self) -> str:
        if self.declared_seniority == self.evidence_seniority:
            return self.effective_seniority
        return f"{self.declared_seniority} -> {self.evidence_seniority}"


def _to_legacy_seniority(level: str) -> LEGACY_SENIORITY_LEVEL:
    level = level.lower().strip() if level else "unknown"
    mapping = {
        "graduate": "new_grad",
        "entry": "junior",
        "entry_level": "junior",
        "associate": "junior",
        "staff": "lead",
        "principal": "lead",
        "manager": "lead",
        "director": "lead",
    }
    level = mapping.get(level, level)
    if level in {"intern", "new_grad", "junior", "mid", "senior", "lead"}:
        return level
    return "unknown"


def _default_eligible_levels(level: LEGACY_SENIORITY_LEVEL) -> list[str]:
    defaults = {
        "intern": ["intern"],
        "new_grad": ["intern", "new_grad", "graduate", "entry", "junior"],
        "junior": ["new_grad", "graduate", "entry", "junior", "mid"],
        "mid": ["junior", "mid", "senior"],
        "senior": ["mid", "senior", "lead"],
        "lead": ["senior", "lead", "staff", "principal", "manager"],
        "unknown": ["new_grad", "graduate", "entry", "junior", "mid"],
    }
    return defaults[level]


def _default_stretch_levels(level: LEGACY_SENIORITY_LEVEL, mode: str) -> list[str]:
    if mode == "strict":
        return []
    defaults = {
        "intern": ["new_grad"] if mode == "stretch" else [],
        "new_grad": ["mid"] if mode == "stretch" else ["junior"],
        "junior": ["senior"] if mode == "stretch" else ["mid"],
        "mid": ["lead"] if mode == "stretch" else ["senior"],
        "senior": ["staff", "principal"] if mode == "stretch" else ["lead"],
        "lead": ["director"] if mode == "stretch" else [],
        "unknown": ["senior"] if mode == "stretch" else ["mid"],
    }
    return defaults[level]


def _default_blocked_levels(level: LEGACY_SENIORITY_LEVEL) -> list[str]:
    defaults = {
        "intern": ["junior", "mid", "senior", "staff", "principal", "lead", "manager", "director"],
        "new_grad": ["senior", "staff", "principal", "lead", "manager", "director"],
        "junior": ["staff", "principal", "manager", "director"],
        "mid": ["principal", "director"],
        "senior": ["director"],
        "lead": [],
        "unknown": ["director"],
    }
    return defaults[level]


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
    assessment: JobAssessment | None = None

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
