"""Job Search：通过 JobSpy 抓取 Indeed / LinkedIn，不依赖浏览器或 Jina。

流程：
  JobSpy 抓取（Indeed + LinkedIn）→ LLM 粗筛
  → 保守预过滤（去重 / 缓存 / 无描述 / 已关闭）→ LLM 批量评估 → 写缓存
"""
from __future__ import annotations

import re
from uuid import uuid4
from typing import Callable

from jobradar import cache
from jobradar.logger import get_logger
from jobradar.llm_backend import DEFAULT_MODELS, LLMConfig, Provider
from jobradar.pipeline_stats import PipelineStats
from jobradar.schemas import CVProfile, SearchSession
from jobradar.search_assessment_stage import flush_assessments
from jobradar.search_prefilter import collect_all_sources, prefilter_jobs
from jobradar.scraping import scrape_sources

logger = get_logger(__name__)

def run_search(
    profile: CVProfile,
    location: str,
    llm: LLMConfig | None = None,
    cv_hash: str = "",
    on_progress: Callable[[str], None] | None = None,
    on_job: Callable[[str], None] | None = None,
    force_refresh: bool = False,
    language: str = "zh",
    limit_per_role: int = 200,
    linkedin_limit_per_role: int = 30,
    hours_old: int | None = 72,
    # 兼容旧参数，优先使用 llm
    provider: Provider = "claude",
    model: str | None = None,
) -> tuple[list[str], PipelineStats]:
    """
    抓取所有注册站点，过滤并写缓存。
    返回 (dedup_key 列表, 本次搜索的管道统计数据)。
    """
    effective_llm = llm or LLMConfig(
        provider=provider,
        model=model or DEFAULT_MODELS.get(provider, ""),
    )
    pipeline_stats = PipelineStats()
    run_id = uuid4().hex
    setattr(pipeline_stats, "run_id", run_id)

    def _cb(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # ── Session 缓存检查 ───────────────────────────────────────────────────────
    active_sources = []
    if limit_per_role > 0:
        active_sources.append("indeed")
    if linkedin_limit_per_role > 0:
        active_sources.append("linkedin")

    session = SearchSession(
        roles=profile.preferred_roles,
        location=location,
        seniority=profile.seniority,
        search_language=profile.search_language,
        sources=active_sources,
    )
    if not force_refresh:
        cached = cache.get_session(session.session_key)
        if cached is not None:
            logger.info("Cache session hit (%d results)", len(cached.job_dedup_keys))
            _cb(f"Cached session hit — skipping scrape ({len(cached.job_dedup_keys)} jobs)")
            if on_job:
                for k in cached.job_dedup_keys:
                    on_job(k)
            pipeline_stats.saved = len(cached.job_dedup_keys)
            return cached.job_dedup_keys, pipeline_stats

    logger.info("Starting search: %s @ %s (seniority=%s)", profile.preferred_roles, location, profile.seniority)
    _cb(f"Starting search: {profile.preferred_roles} @ {location}")

    seen_urls: set[str] = set()
    collected_keys: list[str] = []

    # ── 浏览器直接抓取（注册表中所有站点）────────────────────────────────────
    try:
        scraped = scrape_sources(
            roles=profile.preferred_roles,
            location=location,
            cb=_cb,
            limit_per_query=limit_per_role,
            cv_profile=profile,
            llm=effective_llm,
            linkedin_limit_per_role=linkedin_limit_per_role,
            hours_old=hours_old,
            stats=pipeline_stats,
            run_id=run_id,
        )
        logger.info("Scraped %d jobs, starting prefilter & LLM assessment...", len(scraped))
        keys = _write_scraped(
            scraped, seen_urls, _cb,
            profile=profile,
            llm=effective_llm,
            cv_hash=cv_hash,
            on_job=on_job,
            language=language,
            stats=pipeline_stats,
            run_id=run_id,
        )
        collected_keys.extend(keys)
        logger.info("Scrape done: %d jobs", len(keys))
    except Exception as e:
        logger.warning("Scrape error, skipping: %s", e)
        _cb(f"Scrape skipped due to error: {e}")

    # ── 去重 + 保存 Session ───────────────────────────────────────────────────
    collected_keys = list(dict.fromkeys(collected_keys))
    session.job_dedup_keys = collected_keys
    cache.save_session(session)

    logger.info("Search complete, %d jobs collected", len(collected_keys))
    _cb(f"Search complete — {len(collected_keys)} jobs collected")

    # ── 写管道统计报告 ────────────────────────────────────────────────────────
    try:
        report_path = pipeline_stats.write_report()
        logger.info("Pipeline stats report written: %s", report_path)
    except Exception as e:
        logger.warning("Failed to write pipeline stats report: %s", e)

    return collected_keys, pipeline_stats


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────
# 从职位名称中过滤掉通用词，只保留有区分度的关键词
_ROLE_STOPWORDS = {
    "engineer", "senior", "junior", "lead", "staff", "graduate", "intern",
    "associate", "principal", "manager", "director", "developer", "specialist",
    "analyst", "architect", "consultant", "officer", "head", "founding",
    "and", "or", "the", "of", "in", "at", "ii", "iii", "i",
}

def _build_role_keywords(roles: list[str]) -> set[str]:
    """从 preferred_roles 提取有区分度的关键词，用于过滤不相关职位。"""
    keywords: set[str] = set()
    for role in roles:
        for word in re.split(r"[\s/\-]+", role.lower()):
            if word and word not in _ROLE_STOPWORDS and len(word) > 1:
                keywords.add(word)
    return keywords


def _is_title_relevant(title: str, role_keywords: set[str]) -> bool:
    """判断职位标题是否包含目标角色关键词。"""
    title_words = set(re.split(r"[\s/\-,|@().]+", title.lower()))
    return bool(title_words & role_keywords)


def _over_experience_limit(snippet: str, max_years: int) -> bool:
    matches = re.findall(
        r"(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience|exp\b)",
        snippet,
        re.IGNORECASE,
    )
    return any(int(m) > max_years for m in matches)

def _write_scraped(
    jobs: list[dict],
    seen_urls: set[str],
    cb: Callable[[str], None],
    profile: CVProfile | None = None,
    llm: LLMConfig | None = None,
    cv_hash: str = "",
    on_job: Callable[[str], None] | None = None,
    language: str = "zh",
    stats: PipelineStats | None = None,
    run_id: str = "",
    # 兼容旧参数
    seniority: str = "",
    max_years: int = 99,
    cv_skills: list[str] | None = None,
    cv_summary: str = "",
) -> list[str]:
    """
    将抓取的结构化职位写入缓存。
    阶段一：_prefilter（保守硬过滤）
    阶段二：_flush_assessments（LLM 批量评估 + 写缓存）
    阶段三：多来源合并
    """
    _seniority = profile.seniority if profile else seniority
    _max_years = profile.years_of_experience if profile else max_years
    _cv_skills = profile.skills if profile else (cv_skills or [])
    _cv_summary = profile.summary if profile else cv_summary

    job_all_sources = collect_all_sources(jobs)

    pf = prefilter_jobs(jobs, seen_urls, cb, profile, language=language, run_id=run_id)

    _profile = profile or CVProfile(
        summary=_cv_summary, skills=_cv_skills,
        seniority=_seniority, years_of_experience=_max_years,
        preferred_roles=[], search_language="en",
    )
    keys, llm_rejected, new_saved = flush_assessments(pf, job_all_sources, _profile, llm, cv_hash, cb, on_job, language, run_id=run_id)

    # 阶段三：多来源合并（跨平台重复职位补全 sources 字段）
    for dk, srcs in job_all_sources.items():
        if len(srcs) > 1:
            for s in srcs:
                cache.merge_job_source(dk, s)

    # 填充管道统计
    if stats is not None:
        existing_title_skip = int(getattr(stats, "skip_irrelevant", 0))
        existing_title_relevance_in = int(getattr(stats, "title_relevance_in", 0))
        existing_title_relevance_rejected = int(getattr(stats, "title_relevance_rejected", 0))
        stats.prefilter_in    = pf.total
        stats.skip_dup        = pf.skip_dup
        stats.skip_seniority  = pf.skip_seniority
        stats.skip_irrelevant = existing_title_skip + pf.skip_irrelevant
        stats.title_relevance_in = existing_title_relevance_in + pf.title_relevance_in
        stats.title_relevance_rejected = existing_title_relevance_rejected + pf.title_relevance_rejected
        stats.cache_hit       = pf.cache_hit
        stats.cache_patch     = pf.cache_patch
        stats.skip_no_desc    = pf.skip_no_desc
        stats.skip_closed     = pf.skip_closed
        stats.skip_exp        = pf.skip_exp
        stats.skip_skill      = pf.skip_skill
        stats.llm_assessed    = len(pf.pending) + len(pf.patch_pending)
        stats.llm_rejected    = llm_rejected
        stats.saved           = len(keys)
        stats.new_saved       = new_saved
        stats.by_source       = {src: dict(st) for src, st in pf.source_stats.items()}

    # 汇总日志
    saved = len(keys)
    logger.info(
        "Filter funnel | input=%d dup_skip=%d seniority_skip=%d title_skip=%d cache_hit=%d cache_patch=%d "
        "exp_skip=%d no_desc=%d closed=%d llm_in=%d llm_rejected=%d saved=%d",
        pf.total, pf.skip_dup, pf.skip_seniority, pf.skip_irrelevant, pf.cache_hit, pf.cache_patch,
        pf.skip_exp, pf.skip_no_desc, pf.skip_closed, len(pf.pending) + len(pf.patch_pending),
        llm_rejected, saved,
    )
    cb(
        f"Summary: {pf.total} in → seniority skip {pf.skip_seniority} | title skip {pf.skip_irrelevant} | exp skip {pf.skip_exp} | cache hit {pf.cache_hit} | no description {pf.skip_no_desc} | "
        f"closed {pf.skip_closed} | LLM rejected {llm_rejected} → saved {saved}"
    )
    if pf.source_stats:
        for src, st in sorted(pf.source_stats.items()):
            parts = [f"{step}={st[step]}" for step in ("dup", "skip_seniority", "skip_irrelevant", "skip_exp", "cache_hit", "no_desc", "closed", "llm_rejected") if st.get(step)]
            detail = f"({', '.join(parts)})" if parts else ""
            logger.info("Source [%s]: %d in → %d saved %s", src, st["in"], st["saved"], detail)
        src_summary = " | ".join(f"{src} {st['in']} in → {st['saved']} saved" for src, st in sorted(pf.source_stats.items()))
        cb(f"Source breakdown: {src_summary}")

    return keys
