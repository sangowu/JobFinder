"""
基于 JobSpy 的抓取入口，替换原 scrapers.py 中的 scrape_sources。

与旧版接口完全兼容（相同函数签名），供 agent.py 直接切换 import。
只抓 Indeed，不依赖 Playwright / CDP / 浏览器。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from jobfinder.logger import get_logger
from jobfinder.scraper_jobspy import (
    _LINKEDIN_LOCATION,
    _filter_cards_by_llm,
    scrape_indeed_jobspy_multi,
    scrape_linkedin_jobspy_multi,
)

if TYPE_CHECKING:
    from jobfinder.llm_backend import LLMConfig
    from jobfinder.pipeline_stats import PipelineStats
    from jobfinder.schemas import CVProfile

logger = get_logger(__name__)


def scrape_sources(
    roles: list[str],
    location: str,
    cb: Callable[[str], None] | None = None,
    limit_per_query: int = 200,
    cv_profile: "CVProfile | None" = None,
    llm: "LLMConfig | None" = None,
    # 兼容旧参数
    provider: str = "gemini",
    model: str = "gemini-2.0-flash",
    linkedin_limit_per_role: int = 30,
    hours_old: int | None = 72,
    stats: "PipelineStats | None" = None,
) -> list[dict]:
    """
    用 JobSpy 抓取 Indeed + LinkedIn，合并去重后返回。
    """
    def _cb(msg: str) -> None:
        if cb:
            cb(msg)

    # 解析 LLMConfig（兼容旧参数）
    _provider = provider
    _model    = model
    if llm is not None:
        _provider = llm.provider
        _model    = llm.model

    country = location.strip().split()[0].lower() if location else "ireland"
    linkedin_location = _LINKEDIN_LOCATION.get(country, f"{location.title()}")

    # ── Indeed ────────────────────────────────────────────────────────────────
    raw_indeed: list[dict] = []
    if limit_per_query > 0:
        raw_indeed = scrape_indeed_jobspy_multi(
            roles=roles,
            limit_per_role=limit_per_query,
            country=country,
            hours_old=hours_old,
            cb=cb,
        )
    else:
        _cb("Indeed scraping skipped (limit=0)")

    # ── LinkedIn ──────────────────────────────────────────────────────────────
    raw_linkedin: list[dict] = []
    if linkedin_limit_per_role > 0 and linkedin_location:
        raw_linkedin = scrape_linkedin_jobspy_multi(
            roles=roles,
            limit_per_role=linkedin_limit_per_role,
            location=linkedin_location,
            hours_old=hours_old,
            cb=cb,
        )
    elif linkedin_limit_per_role > 0:
        _cb("LinkedIn scraping skipped: no location mapping for remote")

    # ── 合并 URL 去重 ─────────────────────────────────────────────────────────
    seen: set[str] = {j["url"] for j in raw_indeed}
    raw = list(raw_indeed)
    for job in raw_linkedin:
        if job["url"] not in seen:
            seen.add(job["url"])
            raw.append(job)
    _cb(f"Merged: {len(raw_indeed)} indeed + {len(raw_linkedin)} linkedin = {len(raw)} total")

    # 阶段一数量写入 stats
    if stats is not None:
        stats.scraped_indeed = len(raw_indeed)
        stats.scraped_linkedin = len(raw_linkedin)
        stats.scraped_total = len(raw)

    if not raw:
        _cb("JobSpy: no results returned")
        return []

    # LLM 标题过滤（有 CVProfile 时）
    if cv_profile is not None:
        cards_meta = [
            {"id": i, "title": j["title"], "company": j["company"], "location": j["location"]}
            for i, j in enumerate(raw)
        ]
        logger.info("LLM 标题过滤：共 %d 条待评分", len(cards_meta))
        passing_ids = _filter_cards_by_llm(cards_meta, cv_profile, _provider, _model)
        before = len(raw)
        raw = [j for i, j in enumerate(raw) if i in passing_ids]
        logger.info("LLM title filter done: %d → %d jobs", before, len(raw))
        _cb(f"LLM title filter: {before} → {len(raw)} jobs")
        # 阶段二数量写入 stats
        if stats is not None:
            stats.title_filter_in = before
            stats.title_filter_passed = len(raw)
            stats.title_filter_out = before - len(raw)
    else:
        logger.info("LLM title filter skipped (no CVProfile), keeping %d jobs", len(raw))
        _cb(f"LLM title filter skipped (no CVProfile): keeping {len(raw)} jobs")
        if stats is not None:
            stats.title_filter_in = len(raw)
            stats.title_filter_passed = len(raw)
            stats.title_filter_out = 0

    return raw
