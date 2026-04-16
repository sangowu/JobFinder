from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field

DEFAULT_TTL_DAYS = 7

# 检测职位已关闭的关键词模式
_CLOSED_PATTERN = re.compile(
    r"\b("
    r"applications?\s+(are\s+)?(now\s+)?(closed|ended|no longer accepted)"
    r"|no longer (accepting|available|open)"
    r"|position (has been |is )?(filled|closed|removed)"
    r"|this (job|position|vacancy|role) (is|has been) (closed|expired|filled|removed)"
    r"|job (is\s+)?no longer available"
    r"|vacancy (is\s+)?(closed|filled)"
    r"|(posting|listing|advert|advertisement)\s+(has\s+)?(expired|been removed)"
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
    name: str
    summary: str = Field(description="一句话专业定位")
    skills: list[str] = Field(default_factory=list)
    years_of_experience: int = Field(ge=0)
    preferred_locations: list[str] = Field(default_factory=list)
    preferred_roles: list[str] = Field(default_factory=list)
    seniority: Literal["intern", "new_grad", "junior", "mid", "senior", "lead", "unknown"] = "unknown"
    search_language: str = Field(
        default="en",
        description="搜索词语言，由目标市场决定，如 en / zh / ja",
    )
    search_terms: list[str] = Field(
        default_factory=list,
        description="基于经验等级和目标市场生成的搜索术语，如 ['graduate programme', 'entry level']",
    )


# ─── JobResult ────────────────────────────────────────────────────────────────


class JobAssessment(BaseModel):
    score: int = Field(ge=0, le=10, description="CV 与 JD 整体匹配分 0~10")
    strengths: list[str] = Field(default_factory=list, description="CV 相对于该 JD 的优势")
    weaknesses: list[str] = Field(default_factory=list, description="CV 相对于该 JD 的劣势")
    matched_keywords: list[str] = Field(default_factory=list, description="CV 与 JD 重叠的具体技能/关键词")


# ─── CompanyProfile ───────────────────────────────────────────────────────────


class CompanyProfile(BaseModel):
    size: Literal["startup", "sme", "enterprise", "unknown"] = "unknown"
    industry: str = Field(default="", description="所属行业，如 Technology / AI")
    hq_location: str = Field(default="", description="总部城市（英文）")
    overview: str = Field(default="", description="一到两句话的公司概述（中文）")
    career_page_url: str = Field(default="", description="公司招聘页 URL（若找到）")


class JobResult(BaseModel):
    title: str
    company: str
    location: str = ""
    url: str
    description_snippet: str = ""
    sources: list[str] = Field(default_factory=list)
    date_posted: str = ""          # JobSpy 返回的原始发布日期，如 "2024-04-10"
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    is_complete: bool = True  # False 表示有字段缺失
    assessment: JobAssessment | None = None
    company_profile: CompanyProfile | None = None

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
