from __future__ import annotations

from typing import Callable

from jobradar import cache
from jobradar.assessment import JDAssessment, batch_assess_jds
from jobradar.jd_profile import extract_jd_profile
from jobradar.logger import get_logger
from jobradar.matching import match_job_to_cv
from jobradar.schemas import CVProfile, make_dedup_key
from jobradar.search_prefilter import SOURCE_STATS_KEYS, PrefilterResult
from jobradar.tools import write_cache

logger = get_logger(__name__)


def flush_assessments(
    pf: PrefilterResult,
    job_all_sources: dict[str, list[dict]],
    profile: CVProfile,
    llm,
    cv_hash: str,
    cb: Callable[[str], None],
    on_job: Callable[[str], None] | None,
    language: str,
    run_id: str = "",
) -> tuple[list[str], int, int]:
    def _is_visible_job(job_obj) -> bool:
        return bool(job_obj is not None and job_obj.is_effectively_relevant)

    has_cv = bool(profile.summary and profile.skills)
    keys: list[str] = []
    llm_rejected = 0
    new_saved = 0

    if pf.immediate_keys and has_cv and llm:
        cb(f"Checking explainable scores for {len(pf.immediate_keys)} cached jobs...")

    for key in pf.immediate_keys:
        job_obj = cache.get_job(key, language=language)
        if job_obj is not None and has_cv and llm:
            try:
                jd_profile = extract_jd_profile(job_obj, llm, language=language)
                job_obj.jd_profile = jd_profile
                job_obj.match_score = match_job_to_cv(
                    profile,
                    jd_profile,
                    job_obj.description_snippet,
                    llm,
                    cv_hash=cv_hash,
                    language=language,
                )
            except Exception as exc:
                logger.warning("JD profile extraction/matching failed for cached job %s: %s", key, exc)
        if not _is_visible_job(job_obj):
            logger.debug("Skip cached result after final relevance check: %s", key)
            continue
        keys.append(key)
        if on_job:
            on_job(key)

    if pf.patch_pending and has_cv and llm:
        cb(f"Re-assessing {len(pf.patch_pending)} cached jobs...")
        patch_inputs = [(cached_job.title, cached_job.description_snippet) for cached_job, _ in pf.patch_pending]
        patch_assessments = batch_assess_jds(patch_inputs, profile, llm, language=language)
        for (cached_job, _), assessment in zip(pf.patch_pending, patch_assessments):
            if not assessment.relevant:
                cb(f"Skip (not relevant): {cached_job.title[:50]} — {assessment.reason}")
                logger.info("LLM re-assess rejected: %s | %s", cached_job.title, assessment.reason)
                cache.record_filter_event(
                    run_id=run_id,
                    stage="jd_assessment",
                    title=cached_job.title,
                    company=cached_job.company,
                    location=cached_job.location,
                    url=cached_job.url,
                    reason=assessment.reason,
                    details={"score": assessment.score, "cached": True},
                )
                llm_rejected += 1
            write_cache(
                {
                    "title": cached_job.title,
                    "company": cached_job.company,
                    "location": cached_job.location,
                    "url": cached_job.url,
                    "description_snippet": cached_job.description_snippet,
                    "expires_at": cached_job.expires_at,
                    "is_complete": cached_job.is_complete,
                    "coarse_filter": cached_job.coarse_filter,
                    "assessment": assessment.to_job_assessment(),
                }
            )
            if assessment.relevant:
                try:
                    cached_job = cache.get_job(cached_job.dedup_key) or cached_job
                    jd_profile = extract_jd_profile(cached_job, llm, language=language)
                    match_job_to_cv(profile, jd_profile, cached_job.description_snippet, llm, cv_hash=cv_hash, language=language)
                except Exception as exc:
                    logger.warning("JD profile extraction failed for cached job %s: %s", cached_job.dedup_key, exc)
            if assessment.relevant:
                final_job = cache.get_job(cached_job.dedup_key, language=language)
                if not _is_visible_job(final_job):
                    cb(f"Skip (final match): {cached_job.title[:50]}")
                    logger.info("Final match filtered cached job: %s", cached_job.title)
                    cache.record_filter_event(
                        run_id=run_id,
                        stage="final_match",
                        title=cached_job.title,
                        company=cached_job.company,
                        location=cached_job.location,
                        url=cached_job.url,
                        reason="match recommendation=skip",
                        details={"cached": True},
                    )
                    continue
                keys.append(cached_job.dedup_key)
                if on_job:
                    on_job(cached_job.dedup_key)

    if pf.pending:
        if has_cv and llm:
            cb(f"LLM assessing {len(pf.pending)} jobs...")
            batch_inputs = [(job.get("title", ""), content) for job, content, _ in pf.pending]
            assessments = batch_assess_jds(batch_inputs, profile, llm, language=language)
        else:
            assessments = [
                JDAssessment(
                    relevant=True,
                    reason="无 CV 信息，默认保留",
                    score=0,
                    strengths=[],
                    weaknesses=[],
                    matched_keywords=[],
                )
                for _ in pf.pending
            ]

        for (job, content, expires_at), assessment in zip(pf.pending, assessments):
            title = (job.get("title") or "").strip()
            job_source = job.get("source") or "unknown"
            source_stats = pf.source_stats.setdefault(job_source, {key: 0 for key in SOURCE_STATS_KEYS})
            if not assessment.relevant:
                cb(f"Skip (not relevant): {title[:50]} — {assessment.reason}")
                logger.info("LLM assess rejected: %s | %s", title, assessment.reason)
                cache.record_filter_event(
                    run_id=run_id,
                    stage="jd_assessment",
                    title=title,
                    company=job.get("company", ""),
                    location=job.get("location", ""),
                    source=job_source,
                    url=job.get("url", ""),
                    reason=assessment.reason,
                    details={"score": assessment.score, "cached": False},
                )
                source_stats["llm_rejected"] += 1
                llm_rejected += 1
            else:
                logger.debug("LLM assess matched: %s | score=%d", title, assessment.score)

            job_assessment = assessment.to_job_assessment() if has_cv else None
            dedup_key = make_dedup_key(job.get("company", ""), title)
            raw_sources = job_all_sources.get(
                dedup_key,
                [{"source": job_source, "url": job.get("url", ""), "date_posted": job.get("date_posted", "")}],
            )
            key = write_cache(
                {
                    "title": title,
                    "company": job.get("company", ""),
                    "location": job.get("location", ""),
                    "url": job.get("url", ""),
                    "description_snippet": content,
                    "date_posted": job.get("date_posted", ""),
                    "expires_at": expires_at,
                    "is_complete": job.get("is_complete", True),
                    "coarse_filter": job.get("coarse_filter"),
                    "assessment": job_assessment,
                    "sources": [entry["source"] for entry in raw_sources],
                    "raw_sources": raw_sources,
                }
            )
            if assessment.relevant:
                summary_job = cache.get_job(key)
                if summary_job is not None and llm is not None:
                    try:
                        jd_profile = extract_jd_profile(summary_job, llm, language=language)
                        match_job_to_cv(profile, jd_profile, summary_job.description_snippet, llm, cv_hash=cv_hash, language=language)
                    except Exception as exc:
                        logger.warning("JD profile extraction/matching failed for %s: %s", key, exc)
                final_job = cache.get_job(key, language=language)
                if not _is_visible_job(final_job):
                    cb(f"Skip (final match): {title[:50]}")
                    logger.info("Final match filtered job: %s", title)
                    cache.record_filter_event(
                        run_id=run_id,
                        stage="final_match",
                        title=title,
                        company=job.get("company", ""),
                        location=job.get("location", ""),
                        source=job_source,
                        url=job.get("url", ""),
                        reason="match recommendation=skip",
                        details={"cached": False},
                    )
                    continue
                source_stats["saved"] += 1
                new_saved += 1
                keys.append(key)
                if on_job:
                    on_job(key)
                cb(f"Saved: {title} @ {job.get('company', '?')} [{job_source}]")

    total_assessed = len(pf.pending) + len(pf.patch_pending)
    if total_assessed >= 5 and llm_rejected / total_assessed >= 0.9:
        logger.warning(
            "High LLM rejection rate: %.0f%% (%d/%d rejected) — check CV profile or prompt",
            llm_rejected / total_assessed * 100,
            llm_rejected,
            total_assessed,
        )

    return keys, llm_rejected, new_saved
