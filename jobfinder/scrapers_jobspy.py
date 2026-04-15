"""
基于 JobSpy 的抓取入口，替换原 scrapers.py 中的 scrape_sources。

与旧版接口完全兼容（相同函数签名），供 agent.py 直接切换 import。
只抓 Indeed，不依赖 Playwright / CDP / 浏览器。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from jobfinder.logger import get_logger
from jobfinder.scraper_jobspy import _filter_cards_by_llm, scrape_indeed_jobspy_multi

if TYPE_CHECKING:
    from jobfinder.llm_backend import LLMConfig
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
) -> list[dict]:
    """
    用 JobSpy 抓取 Indeed，返回去重、LLM 过滤后的职位列表。

    参数与旧版 scrapers.scrape_sources 完全相同，agent.py 无需改动调用方式。
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

    # 推断 Indeed country（location 首单词，如 "Ireland" → "ireland"）
    country = location.strip().split()[0].lower() if location else "ireland"

    _cb(f"JobSpy scraping (indeed.com/{country}): {roles}")

    raw = scrape_indeed_jobspy_multi(
        roles=roles,
        limit_per_role=limit_per_query,
        country=country,
        cb=cb,
    )

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
    else:
        logger.info("LLM title filter skipped (no CVProfile), keeping %d jobs", len(raw))
        _cb(f"LLM title filter skipped (no CVProfile): keeping {len(raw)} jobs")

    return raw
