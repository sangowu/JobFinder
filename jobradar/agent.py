"""Job Search：通过 JobSpy 抓取 Indeed / LinkedIn，不依赖浏览器或 Jina。

流程：
  JobSpy 抓取（Indeed + LinkedIn）→ LLM 粗筛
  → 保守预过滤（去重 / 缓存 / 无描述 / 已关闭）→ LLM 批量评估 → 写缓存
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from uuid import uuid4

from jobradar import cache
from jobradar.batch_scheduler import BatchScheduler, ScheduledBatch
from jobradar.llm_backend import DEFAULT_MODELS, LLMConfig, Provider
from jobradar.logger import get_logger
from jobradar.pipeline_stats import PipelineStats
from jobradar.schemas import CVProfile, SearchSession, make_dedup_key
from jobradar.scraping import filter_jobs_by_llm, stream_scrape_source_batches
from jobradar.search_assessment_stage import (
    AssessmentConcurrencyMetrics,
    flush_assessments,
)
from jobradar.search_prefilter import (
    SOURCE_STATS_KEYS,
    PrefilterResult,
    collect_all_sources,
    prefilter_jobs,
)

logger = get_logger(__name__)


class SearchCancelled(Exception):
    """Raised at a cooperative checkpoint when the user stops a search."""


def _resolve_assessment_workers(requested: int | None, provider: str) -> int:
    if requested is not None:
        value = requested
    else:
        default = 1 if provider in {"ollama", "local"} else 5
        raw = os.getenv("ASSESSMENT_WORKERS", str(default)).strip()
        try:
            value = int(raw)
        except ValueError:
            logger.warning("Invalid ASSESSMENT_WORKERS=%r; using %d", raw, default)
            value = default
    if not 1 <= value <= 8:
        raise ValueError("assessment_workers must be between 1 and 8")
    return value


def _merge_prefilter_results(target: PrefilterResult, current: PrefilterResult) -> None:
    for name in (
        "total", "skip_dup", "skip_seniority", "skip_irrelevant", "title_relevance_in",
        "title_relevance_rejected", "cache_hit", "cache_patch", "skip_no_desc",
        "skip_closed", "skip_exp", "skip_skill",
    ):
        setattr(target, name, getattr(target, name) + getattr(current, name))
    for source, values in current.source_stats.items():
        bucket = target.source_stats.setdefault(source, {key: 0 for key in SOURCE_STATS_KEYS})
        for key in SOURCE_STATS_KEYS:
            bucket[key] += values.get(key, 0)


def _run_streaming_pipeline(
    *,
    profile: CVProfile,
    location: str,
    llm: LLMConfig,
    cv_hash: str,
    cb: Callable[[str], None],
    on_job: Callable[[str], None] | None,
    language: str,
    limit_per_role: int,
    linkedin_limit_per_role: int,
    hours_old: int | None,
    stats: PipelineStats,
    run_id: str,
    assessment_workers: int,
) -> list[str]:
    """生产者先落盘 filtered list，再并发评估同一批内存对象。"""
    started_at = time.monotonic()
    scrape_started_at = started_at
    scrape_finished_at: float | None = None
    assessment_started_at: float | None = None
    assessment_finished_at: float | None = None
    first_job_at: float | None = None
    first_job_lock = threading.Lock()

    collected_keys: list[str] = []
    aggregate_pf = PrefilterResult()
    seen_urls: set[str] = set()
    seen_dedup_keys: set[str] = set()
    all_sources: dict[str, list[dict]] = {}
    sources_lock = threading.Lock()
    persistence_elapsed = 0.0
    assessment_batches = 0
    assessment_batch_jobs = 0
    llm_assessed = 0
    llm_rejected = 0
    new_saved = 0
    concurrency_metrics = AssessmentConcurrencyMetrics(workers=assessment_workers)
    cache.prune_search_candidates()

    def _emit_job(key: str) -> None:
        nonlocal first_job_at
        with first_job_lock:
            if first_job_at is None:
                first_job_at = time.monotonic()
        if on_job:
            on_job(key)

    def _source_snapshot(pf: PrefilterResult) -> dict[str, list[dict]]:
        keys = {
            make_dedup_key(str(job.get("company") or ""), str(job.get("title") or ""))
            for job, _, _ in pf.pending
        }
        with sources_lock:
            return {key: list(all_sources.get(key, [])) for key in keys}

    def _cached_filter_dict(job) -> dict:
        return {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "url": job.url,
            "source": job.sources[0] if job.sources else "unknown",
            "description_snippet": job.description_snippet,
        }

    def _process_assessment(
        scheduled: ScheduledBatch[tuple[PrefilterResult, list[str]]],
    ) -> None:
        nonlocal assessment_started_at, assessment_finished_at
        nonlocal assessment_batches, assessment_batch_jobs, llm_assessed, llm_rejected, new_saved
        pf, candidate_keys = scheduled.value
        now = time.monotonic()
        if assessment_started_at is None:
            assessment_started_at = now
        assessment_batches += 1
        assessment_batch_jobs += len(pf.pending) + len(pf.patch_pending) + len(pf.immediate_keys)
        cache.update_search_candidate_status(run_id, candidate_keys, "processing")
        try:
            original_pending = list(pf.pending)
            original_patch = list(pf.patch_pending)
            original_immediate = list(pf.immediate_keys)
            filter_entries: list[tuple[str, object, dict]] = [
                ("pending", item, item[0]) for item in original_pending
            ]
            filter_entries.extend(
                ("patch", item, _cached_filter_dict(item[0])) for item in original_patch
            )
            for key in original_immediate:
                cached_job = cache.get_job(key, language=language)
                if cached_job is not None:
                    filter_entries.append(("immediate", key, _cached_filter_dict(cached_job)))
            filtered_jobs = filter_jobs_by_llm(
                [entry[2] for entry in filter_entries],
                cv_profile=profile,
                llm=llm,
                location=location,
                cb=cb,
                stats=stats,
                run_id=run_id,
            )
            kept_ids = {id(job) for job in filtered_jobs}
            pf.pending = [
                item for kind, item, job in filter_entries
                if kind == "pending" and id(job) in kept_ids
            ]
            pf.patch_pending = [
                item for kind, item, job in filter_entries
                if kind == "patch" and id(job) in kept_ids
            ]
            pf.immediate_keys = [
                item for kind, item, job in filter_entries
                if kind == "immediate" and id(job) in kept_ids
            ]
            llm_assessed += len(pf.pending) + len(pf.patch_pending)

            keys, rejected, saved = flush_assessments(
                pf,
                _source_snapshot(pf),
                profile,
                llm,
                cv_hash,
                cb,
                _emit_job,
                language,
                run_id=run_id,
                evaluation_executor=evaluation_executor,
                assessment_workers=assessment_workers,
                concurrency_metrics=concurrency_metrics,
            )
            collected_keys.extend(keys)
            llm_rejected += rejected
            new_saved += saved
            cache.update_search_candidate_status(run_id, candidate_keys, "completed")
            _merge_prefilter_results(aggregate_pf, pf)
            assessment_finished_at = time.monotonic()
        except Exception:
            try:
                cache.update_search_candidate_status(run_id, candidate_keys, "failed")
            except Exception as status_exc:
                logger.warning("Failed to mark candidate batch as failed: %s", status_exc)
            raise

    def _assessment_tasks() -> Iterator[ScheduledBatch[tuple[PrefilterResult, list[str]]]]:
        nonlocal scrape_finished_at, persistence_elapsed
        for batch_index, scraped_batch in enumerate(stream_scrape_source_batches(
            roles=profile.preferred_roles,
            location=location,
            cb=cb,
            limit_per_query=limit_per_role,
            linkedin_limit_per_role=linkedin_limit_per_role,
            hours_old=hours_old,
            stats=stats,
        )):
            batch_sources = collect_all_sources(scraped_batch)
            with sources_lock:
                for dedup_key, entries in batch_sources.items():
                    bucket = all_sources.setdefault(dedup_key, [])
                    known = {item["source"] for item in bucket}
                    bucket.extend(item for item in entries if item["source"] not in known)

            pf = prefilter_jobs(
                scraped_batch,
                seen_urls,
                cb,
                profile,
                language=language,
                run_id=run_id,
                seen_dedup_keys=seen_dedup_keys,
            )
            candidate_jobs = [job for job, _, _ in pf.pending]
            persisted_at = time.monotonic()
            candidate_keys = cache.save_search_candidates(run_id, candidate_jobs)
            persistence_elapsed += time.monotonic() - persisted_at

            if pf.pending or pf.patch_pending or pf.immediate_keys:
                ready_at = time.monotonic()
                yield ScheduledBatch(
                    batch_id=f"{run_id}-{batch_index:04d}",
                    value=(pf, candidate_keys),
                    ready_at=ready_at,
                    item_count=len(pf.pending) + len(pf.patch_pending) + len(pf.immediate_keys),
                )
        scrape_finished_at = time.monotonic()

    with ThreadPoolExecutor(
        max_workers=assessment_workers,
        thread_name_prefix="jobradar-evaluation",
    ) as evaluation_executor:
        scheduler_metrics = BatchScheduler[tuple[PrefilterResult, list[str]]]("streaming").run(
            _assessment_tasks(),
            _process_assessment,
        )

    with sources_lock:
        source_items = [(key, list(entries)) for key, entries in all_sources.items()]
    for dedup_key, entries in source_items:
        if len(entries) > 1:
            for entry in entries:
                cache.merge_job_raw_source(dedup_key, entry)

    finished_at = time.monotonic()
    scrape_finished_at = scrape_finished_at or finished_at
    assessment_finished_at = assessment_finished_at or scrape_finished_at
    stats.pipeline_elapsed = round(finished_at - started_at, 4)
    stats.scrape_elapsed = round(scrape_finished_at - scrape_started_at, 4)
    if assessment_started_at is not None:
        stats.assessment_elapsed = round(assessment_finished_at - assessment_started_at, 4)
        stats.overlap_elapsed = round(
            max(0.0, min(scrape_finished_at, assessment_finished_at) - assessment_started_at),
            4,
        )
    stats.time_to_first_job = round(first_job_at - started_at, 4) if first_job_at is not None else None
    stats.persistence_elapsed = round(persistence_elapsed, 4)
    stats.assessment_batches = assessment_batches
    stats.assessment_batch_jobs = assessment_batch_jobs
    stats.queue_peak = scheduler_metrics.queue_peak
    stats.queue_wait_avg = round(scheduler_metrics.queue_wait_avg, 4)
    stats.queue_wait_p50 = round(scheduler_metrics.queue_wait_p50, 4)
    stats.queue_wait_p95 = round(scheduler_metrics.queue_wait_p95, 4)
    stats.assessment_workers = assessment_workers
    stats.evaluation_tasks = concurrency_metrics.submitted
    stats.evaluation_completed = concurrency_metrics.completed
    stats.evaluation_failed = concurrency_metrics.failed
    stats.evaluation_peak_inflight = concurrency_metrics.peak_inflight

    stats.prefilter_in = aggregate_pf.total
    stats.skip_dup = aggregate_pf.skip_dup
    stats.skip_seniority = aggregate_pf.skip_seniority
    stats.skip_irrelevant += aggregate_pf.skip_irrelevant
    stats.cache_hit = aggregate_pf.cache_hit
    stats.cache_patch = aggregate_pf.cache_patch
    stats.skip_no_desc = aggregate_pf.skip_no_desc
    stats.skip_closed = aggregate_pf.skip_closed
    stats.skip_exp = aggregate_pf.skip_exp
    stats.skip_skill = aggregate_pf.skip_skill
    stats.llm_assessed = llm_assessed
    stats.llm_rejected = llm_rejected
    unique_keys = list(dict.fromkeys(collected_keys))
    stats.saved = len(unique_keys)
    stats.new_saved = new_saved
    stats.by_source = {source: dict(values) for source, values in aggregate_pf.source_stats.items()}

    logger.info(
        "Streaming pipeline | batches=%d queued_jobs=%d scrape=%.2fs assess=%.2fs overlap=%.2fs "
        "persist=%.3fs first_job=%s saved=%d",
        assessment_batches,
        assessment_batch_jobs,
        stats.scrape_elapsed,
        stats.assessment_elapsed,
        stats.overlap_elapsed,
        stats.persistence_elapsed,
        f"{stats.time_to_first_job:.2f}s" if stats.time_to_first_job is not None else "none",
        len(unique_keys),
    )
    return unique_keys


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
    control_checkpoint: Callable[[], None] | None = None,
    assessment_workers: int | None = None,
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
    effective_assessment_workers = _resolve_assessment_workers(assessment_workers, effective_llm.provider)
    pipeline_stats = PipelineStats()
    run_id = uuid4().hex
    setattr(pipeline_stats, "run_id", run_id)

    def _cb(msg: str) -> None:
        if control_checkpoint:
            control_checkpoint()
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
                    if control_checkpoint:
                        control_checkpoint()
                    on_job(k)
            pipeline_stats.saved = len(cached.job_dedup_keys)
            return cached.job_dedup_keys, pipeline_stats

    logger.info("Starting search: %s @ %s (seniority=%s)", profile.preferred_roles, location, profile.seniority)
    _cb(f"Starting search: {profile.preferred_roles} @ {location}")

    collected_keys: list[str] = []

    # ── 流式抓取 + 异步评估 ─────────────────────────────────────────────────
    try:
        collected_keys = _run_streaming_pipeline(
            profile=profile,
            location=location,
            cb=_cb,
            llm=effective_llm,
            cv_hash=cv_hash,
            on_job=on_job,
            language=language,
            limit_per_role=limit_per_role,
            linkedin_limit_per_role=linkedin_limit_per_role,
            hours_old=hours_old,
            stats=pipeline_stats,
            run_id=run_id,
            assessment_workers=effective_assessment_workers,
        )
        logger.info("Streaming scrape and assessment done: %d jobs", len(collected_keys))
    except SearchCancelled:
        raise
    except Exception as e:
        logger.error("Streaming search pipeline failed: %s", e, exc_info=True)
        raise

    # ── 去重 + 保存 Session ───────────────────────────────────────────────────
    if control_checkpoint:
        control_checkpoint()
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
                cache.merge_job_raw_source(dk, s)

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
