"""职位工具函数：Jina 抓取、关闭检测、缓存读写、失败 URL 管理。"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

import requests

from jobradar import cache
from jobradar.logger import get_logger
from jobradar.schemas import JobAssessment, JobResult, make_dedup_key

log = get_logger(__name__)

# ─── Jina Reader 抓取全文 ─────────────────────────────────────────────────────

_VERIFICATION_SIGNALS = (
    "just a moment",
    "enable javascript and cookies",
    "checking your browser",
    "please wait",
    "cloudflare",
    "ddos protection",
    "ray id",
)


def is_verification_page(content: str) -> bool:
    """判断抓取内容是否为 Cloudflare / 人机验证页而非真实 JD。"""
    if not content or len(content) < 200:
        return True
    sample = content[:1000].lower()
    return sum(sig in sample for sig in _VERIFICATION_SIGNALS) >= 2


def fetch_page(url: str, timeout: int = 15) -> str:
    """
    通过 Jina Reader 抓取网页正文，返回纯文本。
    失败或返回验证页时返回空字符串。
    """
    try:
        log.debug("fetch_page: %s", url)
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            text = resp.text[:15000]
            if is_verification_page(text):
                log.warning("fetch_page 返回验证页，跳过：%s", url)
                return ""
            log.debug("fetch_page 成功，长度 %d 字符", len(resp.text))
            return text
        log.warning("fetch_page 失败：%s %s", resp.status_code, url)
        return ""
    except Exception as e:
        log.warning("fetch_page 异常：%s - %s", url, e)
        return ""


# ─── 主动验证职位是否仍有效 ──────────────────────────────────────────────────

_INACTIVE_PATTERN = re.compile(
    r"\b("
    r"(this\s+)?(job|position|vacancy|role|listing|posting)\s+(is\s+)?(no longer|not)\s+(available|accepting|open|active)"
    r"|applications?\s+(are\s+)?(now\s+)?(closed|no longer being accepted)"
    r"|no longer (accepting|taking)\s+applications?"
    r"|(position|vacancy|role)\s+(has been\s+)?(filled|closed|removed)"
    r"|this\s+(role|position|job)\s+has\s+(been\s+)?(filled|closed|expired)"
    r"|sorry[^.]*no longer available"
    r"|recruitment\s+(for\s+this\s+(role|position)\s+)?(has\s+)?(closed|ended|finished)"
    r"|expired on indeed"
    r"|this exact role may not be open"
    r"|posting is to advertise potential job opportunities"
    r")\b",
    re.IGNORECASE,
)


def verify_job_active(url: str) -> dict:
    """
    通过 Jina Reader 抓取职位页面，判断是否仍在招募。
    返回 {"active": bool, "reason": str}
    """
    text = fetch_page(url)
    if not text:
        return {"active": False, "reason": "fetch_failed"}
    if _INACTIVE_PATTERN.search(text):
        return {"active": False, "reason": "posting_closed"}
    return {"active": True, "reason": "ok"}


# ─── 失败 URL 管理 ────────────────────────────────────────────────────────────


def check_failed_urls(urls: list[str]) -> list[str]:
    """返回 urls 中已在失败黑名单内的 URL 列表。"""
    failed = cache.get_failed_urls(urls)
    return list(failed)


def record_failed_url(url: str, reason: str) -> None:
    """记录失败 URL 到黑名单。"""
    cache.record_failed_url(url, reason)


# ─── 缓存读写 ─────────────────────────────────────────────────────────────────


def read_cache(session_key: str) -> dict | None:
    """
    读取 SearchSession 缓存。
    返回 {session, jobs} 或 None（未命中/已过期）。
    """
    session = cache.get_session(session_key)
    if session is None:
        return None
    jobs = cache.get_jobs_by_keys(session.job_dedup_keys)
    return {"session": session.model_dump(mode="json"), "jobs": [j.model_dump(mode="json") for j in jobs]}


def write_cache(job_data: dict, session_key: str | None = None) -> str:
    """
    写入单条 JobResult 到缓存，返回 dedup_key。
    job_data 字段：title, company, location, url, description_snippet,
                   sources, expires_at（可选）, is_complete
    """
    url = job_data.get("url", "")
    sources = job_data.get("sources") or []
    if not sources and url:
        domain = _extract_domain(url)
        if domain:
            sources = [domain]

    raw_assessment = job_data.get("assessment")
    assessment: JobAssessment | None = None
    if isinstance(raw_assessment, JobAssessment):
        assessment = raw_assessment
    elif isinstance(raw_assessment, dict):
        assessment = JobAssessment.model_validate(raw_assessment)
    elif raw_assessment is not None and hasattr(raw_assessment, "model_dump"):
        assessment = JobAssessment.model_validate(raw_assessment.model_dump())

    raw_sources = job_data.get("raw_sources") or []
    if not raw_sources and sources:
        raw_sources = [{"source": sources[0], "url": url, "date_posted": job_data.get("date_posted", "")}]

    job = JobResult(
        title=job_data.get("title", ""),
        company=job_data.get("company", ""),
        location=job_data.get("location", ""),
        url=url,
        description_snippet=job_data.get("description_snippet", ""),
        sources=sources,
        raw_sources=raw_sources,
        date_posted=job_data.get("date_posted", ""),
        expires_at=_parse_date(job_data.get("expires_at")),
        is_complete=job_data.get("is_complete", True),
        assessment=assessment,
    )
    cache.save_job(job)
    log.info("write_cache: %s @ %s [%s]", job.title, job.company, job.dedup_key)
    return job.dedup_key


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except Exception:
        return ""


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from dateutil import parser as dateutil_parser
        return dateutil_parser.parse(value)
    except Exception:
        return None
