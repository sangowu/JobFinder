"""Job Search：通过 JobSpy 抓取 Indeed / LinkedIn，不依赖浏览器或 Jina。

流程：
  JobSpy 抓取（Indeed + LinkedIn）→ LLM 标题过滤
  → 年资过滤 → 相关性过滤 → 缓存命中检查 → LLM 批量评估 → 写缓存
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from jobfinder import cache
from jobfinder.assessment import JDAssessment, batch_assess_jds
from jobfinder.filters import is_seniority_ok
from jobfinder.logger import get_logger
from jobfinder.llm_backend import DEFAULT_MODELS, LLMConfig, Provider
from jobfinder.pipeline_stats import PipelineStats
from jobfinder.schemas import CVProfile, is_closed_posting, make_dedup_key, SearchSession
from jobfinder.scraping import scrape_sources
from jobfinder.tools import record_failed_url, write_cache

logger = get_logger(__name__)


def run_search(
    profile: CVProfile,
    location: str,
    llm: LLMConfig | None = None,
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

    def _cb(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # ── Session 缓存检查 ───────────────────────────────────────────────────────
    active_sources = []
    if limit_per_role > 0:          active_sources.append("indeed")
    if linkedin_limit_per_role > 0: active_sources.append("linkedin")

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
        )
        logger.info("Scraped %d jobs, starting filter & LLM assessment...", len(scraped))
        role_keywords = _build_role_keywords(profile.preferred_roles)
        keys = _write_scraped(
            scraped, seen_urls, _cb,
            role_keywords=role_keywords,
            profile=profile,
            llm=effective_llm,
            on_job=on_job,
            language=language,
            stats=pipeline_stats,
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

# 上下文限定的截止日期正则：必须有"截止语义词 + 日期"的组合，避免误匹配其他数字
_DEADLINE_PATTERN = re.compile(
    r"(?:"
    r"closing\s+date"
    r"|apply\s+by"
    r"|applied\s+by"
    r"|deadline"
    r"|applications?\s+close[sd]?"
    r"|closes?\s+on"
    r"|expir(?:es?|ing)\s+on"
    r"|last\s+(?:date\s+to\s+apply|day\s+to\s+apply|application\s+date)"
    r"|position\s+closes?"
    r"|vacancy\s+closes?"
    r"|accepting\s+applications?\s+until"
    r"|applications?\s+accepted\s+until"
    r"|submit\s+(?:your\s+)?application\s+by"
    r")"
    r"[\s:–\-]*"
    r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}"   # 30/04/2026 或 30-04-26
    r"|\d{1,2}\s+\w+\s+\d{4}"                     # 30 April 2026
    r"|\w+\s+\d{1,2},?\s+\d{4})",                 # April 30, 2026
    re.IGNORECASE,
)

# 从职位名称中过滤掉通用词，只保留有区分度的关键词
_ROLE_STOPWORDS = {
    "engineer", "senior", "junior", "lead", "staff", "graduate", "intern",
    "associate", "principal", "manager", "director", "developer", "specialist",
    "analyst", "architect", "consultant", "officer", "head", "founding",
    "and", "or", "the", "of", "in", "at", "ii", "iii", "i",
}

_SS_KEYS = ("in", "dup", "seniority", "irrelevant", "cache_hit", "no_desc", "closed", "exp", "skill", "llm_rejected", "saved")


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


def _collect_all_sources(jobs: list[dict]) -> dict[str, list[dict]]:
    """预计算每个 dedup_key 对应的全部来源（用于多来源写入）。"""
    result: dict[str, list[dict]] = {}
    for j in jobs:
        dk = make_dedup_key((j.get("company") or "").strip(), (j.get("title") or "").strip())
        entry = {
            "source": j.get("source") or "unknown",
            "url": j.get("url") or "",
            "date_posted": j.get("date_posted") or "",
        }
        if dk not in result:
            result[dk] = []
        if not any(e["source"] == entry["source"] for e in result[dk]):
            result[dk].append(entry)
    return result


@dataclass
class _PrefilterResult:
    immediate_keys: list[str] = field(default_factory=list)
    pending: list[tuple[dict, str, str | None]] = field(default_factory=list)   # (job, content, expires_at)
    patch_pending: list[tuple[object, str]] = field(default_factory=list)        # (cached_job, content)
    total: int = 0
    skip_dup: int = 0
    skip_seniority: int = 0
    skip_irrelevant: int = 0
    cache_hit: int = 0
    cache_patch: int = 0
    skip_no_desc: int = 0
    skip_closed: int = 0
    skip_exp: int = 0
    skip_skill: int = 0
    source_stats: dict[str, dict[str, int]] = field(default_factory=dict)


def _prefilter(
    jobs: list[dict],
    seen_urls: set[str],
    cb: Callable[[str], None],
    role_keywords: set[str] | None,
    seniority: str,
    max_years: int,
    skill_keywords: set[str],
    cv_summary: str,
    cv_skills: list[str],
) -> _PrefilterResult:
    """7 步预过滤漏斗，返回分类后的三个列表及统计数据。"""
    r = _PrefilterResult()
    seen_dedup_keys: set[str] = set()

    for job in jobs:
        r.total += 1
        src = job.get("source") or "unknown"
        ss = r.source_stats.setdefault(src, {k: 0 for k in _SS_KEYS})
        ss["in"] += 1

        url = job.get("url", "")
        if not url or url in seen_urls:
            ss["dup"] += 1; r.skip_dup += 1; continue
        seen_urls.add(url)

        title = (job.get("title") or "").strip()
        if not title:
            ss["dup"] += 1; r.skip_dup += 1; continue

        # 1a. 跨来源 dedup
        company = (job.get("company") or "").strip()
        dedup_key = make_dedup_key(company, title)
        if dedup_key in seen_dedup_keys:
            logger.debug("Skip (cross-source dedup): %s @ %s", title, company)
            ss["dup"] += 1; r.skip_dup += 1; continue
        seen_dedup_keys.add(dedup_key)

        # 1b. 年资过滤
        if seniority and not is_seniority_ok(title, seniority):
            logger.debug("Skip (seniority mismatch): %s", title)
            cb(f"Skip (seniority mismatch): {title[:60]}")
            ss["seniority"] += 1; r.skip_seniority += 1; continue

        # 2. 相关性过滤
        if role_keywords and not _is_title_relevant(title, role_keywords):
            logger.debug("Skip (irrelevant title): %s", title)
            cb(f"Skip (irrelevant title): {title[:60]}")
            ss["irrelevant"] += 1; r.skip_irrelevant += 1; continue

        # 3. URL 缓存命中检查
        cached_job = cache.get_job_by_url(url)
        if cached_job is not None and not cached_job.is_expired:
            if cached_job.assessment is not None:
                if not cached_job.assessment.is_relevant:
                    logger.debug("URL cache hit (rejected), skip: %s", title)
                    ss["cache_hit"] += 1; r.cache_hit += 1; continue
                logger.debug("URL cache hit, skip fetch+LLM: %s", title)
                r.immediate_keys.append(cached_job.dedup_key)
                ss["cache_hit"] += 1; r.cache_hit += 1; continue
            if not (cv_summary and cv_skills):
                r.immediate_keys.append(cached_job.dedup_key)
                ss["cache_hit"] += 1; r.cache_hit += 1; continue
            logger.debug("URL cache hit, pending LLM re-assess: %s", title)
            r.patch_pending.append((cached_job, cached_job.description_snippet))
            r.cache_patch += 1; continue

        # 4. 获取 JD 内容
        content = (job.get("description_snippet") or "").strip()
        if not content:
            logger.debug("Skip (no description): %s", title)
            cb(f"Skip (no description): {title[:50]}")
            ss["no_desc"] += 1; r.skip_no_desc += 1; continue

        # 5. 关闭检测
        if is_closed_posting(content):
            record_failed_url(url, "posting_closed")
            cb(f"Skip (posting closed): {url[:70]}")
            logger.info("Posting closed: %s", url)
            ss["closed"] += 1; r.skip_closed += 1; continue

        # 6. 经验年限检查
        if _over_experience_limit(content, max_years):
            cb(f"Skip (experience requirement too high): {title[:60]}")
            logger.debug("Experience requirement too high: %s", title)
            ss["exp"] += 1; r.skip_exp += 1; continue

        # 7. 技能关键词预筛
        if skill_keywords and not any(s in content.lower() for s in skill_keywords):
            cb(f"Skip (skill mismatch): {title[:60]}")
            logger.debug("Skill mismatch: %s", title)
            ss["skill"] += 1; r.skip_skill += 1; continue

        expires_at = job.get("expires_at")
        if not expires_at:
            m = _DEADLINE_PATTERN.search(content[:1000])
            if m:
                expires_at = m.group(1).strip()

        r.pending.append((job, content, expires_at))

    return r


def _flush_assessments(
    pf: _PrefilterResult,
    job_all_sources: dict[str, list[dict]],
    profile: CVProfile,
    llm: LLMConfig | None,
    cb: Callable[[str], None],
    on_job: Callable[[str], None] | None,
    language: str,
) -> tuple[list[str], int]:
    """
    运行 LLM 评估并写缓存，返回 (saved_keys, llm_rejected_count)。
    """
    has_cv = bool(profile.summary and profile.skills)
    keys: list[str] = []
    llm_rejected = 0

    for k in pf.immediate_keys:
        keys.append(k)
        if on_job:
            on_job(k)

    # patch：缓存命中但缺 assessment
    if pf.patch_pending and has_cv and llm:
        cb(f"Re-assessing {len(pf.patch_pending)} cached jobs...")
        patch_inputs = [(cj.title, cj.description_snippet) for cj, _ in pf.patch_pending]
        patch_assessments = batch_assess_jds(patch_inputs, profile, llm, language=language)
        for (cached_job, _), assessment in zip(pf.patch_pending, patch_assessments):
            if not assessment.relevant:
                cb(f"Skip (not relevant): {cached_job.title[:50]} — {assessment.reason}")
                logger.info("LLM re-assess rejected: %s | %s", cached_job.title, assessment.reason)
                llm_rejected += 1
            write_cache({
                "title": cached_job.title,
                "company": cached_job.company,
                "location": cached_job.location,
                "url": cached_job.url,
                "description_snippet": cached_job.description_snippet,
                "expires_at": cached_job.expires_at,
                "is_complete": cached_job.is_complete,
                "assessment": assessment.to_job_assessment(),
            })
            if assessment.relevant:
                keys.append(cached_job.dedup_key)
                if on_job:
                    on_job(cached_job.dedup_key)

    # 新抓取职位批量评估
    if pf.pending:
        if has_cv and llm:
            cb(f"LLM assessing {len(pf.pending)} jobs...")
            batch_inputs = [(job.get("title", ""), content) for job, content, _ in pf.pending]
            assessments = batch_assess_jds(batch_inputs, profile, llm, language=language)
        else:
            assessments = [
                JDAssessment(
                    relevant=True, reason="无 CV 信息，默认保留",
                    score=0, strengths=[], weaknesses=[], matched_keywords=[],
                )
                for _ in pf.pending
            ]

        for (job, content, expires_at), assessment in zip(pf.pending, assessments):
            title = (job.get("title") or "").strip()
            job_src = job.get("source") or "unknown"
            job_ss = pf.source_stats.setdefault(job_src, {k: 0 for k in _SS_KEYS})
            if not assessment.relevant:
                cb(f"Skip (not relevant): {title[:50]} — {assessment.reason}")
                logger.info("LLM assess rejected: %s | %s", title, assessment.reason)
                job_ss["llm_rejected"] += 1
                llm_rejected += 1
            else:
                logger.debug("LLM assess matched: %s | score=%d", title, assessment.score)

            job_assessment = assessment.to_job_assessment() if has_cv else None
            dedup_key = make_dedup_key(job.get("company", ""), title)
            raw_srcs = job_all_sources.get(dedup_key, [{"source": job_src, "url": job.get("url", ""), "date_posted": job.get("date_posted", "")}])
            key = write_cache({
                "title": title,
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "url": job.get("url", ""),
                "description_snippet": content,
                "date_posted": job.get("date_posted", ""),
                "expires_at": expires_at,
                "is_complete": job.get("is_complete", True),
                "assessment": job_assessment,
                "sources": [e["source"] for e in raw_srcs],
                "raw_sources": raw_srcs,
            })
            if assessment.relevant:
                job_ss["saved"] += 1
                keys.append(key)
                if on_job:
                    on_job(key)
                cb(f"Saved: {title} @ {job.get('company', '?')} [{job_src}]")

    return keys, llm_rejected


def _write_scraped(
    jobs: list[dict],
    seen_urls: set[str],
    cb: Callable[[str], None],
    role_keywords: set[str] | None = None,
    profile: CVProfile | None = None,
    llm: LLMConfig | None = None,
    on_job: Callable[[str], None] | None = None,
    language: str = "zh",
    stats: PipelineStats | None = None,
    # 兼容旧参数
    seniority: str = "",
    max_years: int = 99,
    cv_skills: list[str] | None = None,
    cv_summary: str = "",
) -> list[str]:
    """
    将抓取的结构化职位写入缓存。
    阶段一：_prefilter（7 步漏斗）
    阶段二：_flush_assessments（LLM 批量评估 + 写缓存）
    阶段三：多来源合并
    """
    _seniority  = profile.seniority          if profile else seniority
    _max_years  = profile.years_of_experience if profile else max_years
    _cv_skills  = profile.skills             if profile else (cv_skills or [])
    _cv_summary = profile.summary            if profile else cv_summary

    skill_keywords: set[str] = set()
    for s in _cv_skills:
        skill_keywords.update(w.lower() for w in re.split(r"[\s/\-,]+", s) if len(w) > 1)

    job_all_sources = _collect_all_sources(jobs)

    pf = _prefilter(
        jobs, seen_urls, cb,
        role_keywords=role_keywords,
        seniority=_seniority,
        max_years=_max_years,
        skill_keywords=skill_keywords,
        cv_summary=_cv_summary,
        cv_skills=_cv_skills,
    )

    _profile = profile or CVProfile(
        summary=_cv_summary, skills=_cv_skills,
        seniority=_seniority, years_of_experience=_max_years,
        preferred_roles=[], search_language="en",
    )
    keys, llm_rejected = _flush_assessments(pf, job_all_sources, _profile, llm, cb, on_job, language)

    # 阶段三：多来源合并（跨平台重复职位补全 sources 字段）
    for dk, srcs in job_all_sources.items():
        if len(srcs) > 1:
            for s in srcs:
                cache.merge_job_source(dk, s)

    # 填充管道统计
    if stats is not None:
        stats.prefilter_in    = pf.total
        stats.skip_dup        = pf.skip_dup
        stats.skip_seniority  = pf.skip_seniority
        stats.skip_irrelevant = pf.skip_irrelevant
        stats.cache_hit       = pf.cache_hit
        stats.cache_patch     = pf.cache_patch
        stats.skip_no_desc    = pf.skip_no_desc
        stats.skip_closed     = pf.skip_closed
        stats.skip_exp        = pf.skip_exp
        stats.skip_skill      = pf.skip_skill
        stats.llm_assessed    = len(pf.pending) + len(pf.patch_pending)
        stats.llm_rejected    = llm_rejected
        stats.saved           = len(keys)
        stats.by_source       = {src: dict(st) for src, st in pf.source_stats.items()}

    # 汇总日志
    saved = len(keys)
    logger.info(
        "Filter funnel | input=%d dup_skip=%d seniority=%d irrelevant=%d cache_hit=%d cache_patch=%d "
        "no_desc=%d closed=%d exp_limit=%d skill_mismatch=%d llm_in=%d llm_rejected=%d saved=%d",
        pf.total, pf.skip_dup, pf.skip_seniority, pf.skip_irrelevant,
        pf.cache_hit, pf.cache_patch, pf.skip_no_desc, pf.skip_closed,
        pf.skip_exp, pf.skip_skill, len(pf.pending) + len(pf.patch_pending),
        llm_rejected, saved,
    )
    cb(
        f"Summary: {pf.total} in → seniority {pf.skip_seniority} | irrelevant {pf.skip_irrelevant} | "
        f"cache hit {pf.cache_hit} | no description {pf.skip_no_desc} | closed {pf.skip_closed} | "
        f"exp limit {pf.skip_exp} | skill mismatch {pf.skip_skill} | LLM rejected {llm_rejected} → saved {saved}"
    )
    if pf.source_stats:
        for src, st in sorted(pf.source_stats.items()):
            parts = [f"{step}={st[step]}" for step in ("dup", "seniority", "irrelevant", "cache_hit", "no_desc", "closed", "exp", "skill", "llm_rejected") if st.get(step)]
            detail = f"({', '.join(parts)})" if parts else ""
            logger.info("Source [%s]: %d in → %d saved %s", src, st["in"], st["saved"], detail)
        src_summary = " | ".join(f"{src} {st['in']} in → {st['saved']} saved" for src, st in sorted(pf.source_stats.items()))
        cb(f"Source breakdown: {src_summary}")

    return keys
