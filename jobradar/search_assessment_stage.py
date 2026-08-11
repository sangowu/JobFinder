from __future__ import annotations

import threading
import time
from concurrent.futures import Executor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from jobradar import cache
from jobradar.assessment import (
    JDAssessment,
    batch_assess_jds,
    jd_assessment_prompt_version,
)
from jobradar.jd_profile import jd_profile_prompt_version
from jobradar.job_evaluation import evaluate_job_once, job_evaluation_prompt_version
from jobradar.logger import get_logger
from jobradar.matching import match_job_to_cv, match_prompt_version
from jobradar.schemas import CVProfile, JDProfile, JobResult, MatchScore, make_dedup_key
from jobradar.search_prefilter import SOURCE_STATS_KEYS, PrefilterResult
from jobradar.tools import write_cache

logger = get_logger(__name__)


@dataclass(frozen=True)
class JobEvaluationTask:
    """One persisted job whose profile and CV match can be evaluated independently."""

    key: str
    job: JobResult
    full_jd: str
    kind: str
    source: str = "unknown"


@dataclass(frozen=True)
class JobEvaluationOutcome:
    task: JobEvaluationTask
    jd_profile: JDProfile
    match_score: MatchScore
    profile_prompt_version: str = ""


@dataclass
class AssessmentConcurrencyMetrics:
    workers: int = 1
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    peak_inflight: int = 0
    commit_elapsed: float = 0.0
    _active: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_submission(self, task_count: int) -> None:
        with self._lock:
            self.submitted += task_count

    def record_started(self) -> None:
        with self._lock:
            self._active += 1
            self.peak_inflight = max(self.peak_inflight, self._active)

    def record_finished(self) -> None:
        with self._lock:
            self._active -= 1

    def record_completion(self, *, failed: bool) -> None:
        with self._lock:
            self.completed += 1
            if failed:
                self.failed += 1

    def record_commit(self, elapsed: float) -> None:
        with self._lock:
            self.commit_elapsed += elapsed


def _evaluate_job(
    task: JobEvaluationTask,
    profile: CVProfile,
    llm,
    cv_hash: str,
    language: str,
    metrics: AssessmentConcurrencyMetrics,
) -> JobEvaluationOutcome:
    metrics.record_started()
    try:
        standalone_version = jd_profile_prompt_version(language)
        combined_version = job_evaluation_prompt_version(language)
        jd_profile = cache.get_jd_profile(
            task.key,
            task.full_jd,
            prompt_version=(standalone_version, combined_version),
        )
        if jd_profile is None:
            jd_profile, match_score = evaluate_job_once(
                task.job,
                profile,
                llm,
                cv_hash=cv_hash,
                language=language,
            )
            profile_prompt_version = combined_version
        else:
            match_score = match_job_to_cv(
                profile,
                jd_profile,
                task.full_jd,
                llm,
                cv_hash=cv_hash,
                language=language,
                persist=False,
            )
            # Re-persisting a reused profile must not relabel who produced it.
            profile_prompt_version = (
                cache.get_jd_profile_prompt_version(task.key) or standalone_version
            )
        return JobEvaluationOutcome(
            task=task,
            jd_profile=jd_profile,
            match_score=match_score,
            profile_prompt_version=profile_prompt_version,
        )
    finally:
        metrics.record_finished()


def _commit_job_evaluation(outcome: JobEvaluationOutcome, llm, language: str) -> None:
    task = outcome.task
    cache.save_job_evaluation(
        profile=outcome.jd_profile,
        match=outcome.match_score,
        description=task.full_jd,
        model_name=f"{llm.provider}/{llm.model}",
        profile_prompt_version=outcome.profile_prompt_version or jd_profile_prompt_version(language),
        match_prompt_version=match_prompt_version(language),
    )
    task.job.jd_profile = outcome.jd_profile
    task.job.match_score = outcome.match_score


def _evaluate_jobs(
    tasks: list[JobEvaluationTask],
    *,
    profile: CVProfile,
    llm,
    cv_hash: str,
    language: str,
    executor: Executor | None,
    workers: int,
    metrics: AssessmentConcurrencyMetrics,
) -> list[tuple[JobEvaluationTask, Exception | None]]:
    if not tasks:
        return []
    if executor is None:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jobradar-evaluation") as local_executor:
            return _evaluate_jobs(
                tasks,
                profile=profile,
                llm=llm,
                cv_hash=cv_hash,
                language=language,
                executor=local_executor,
                workers=workers,
                metrics=metrics,
            )

    metrics.record_submission(len(tasks))
    futures = {
        executor.submit(_evaluate_job, task, profile, llm, cv_hash, language, metrics): task
        for task in tasks
    }
    completed: list[tuple[JobEvaluationTask, Exception | None]] = []
    for future in as_completed(futures):
        task = futures[future]
        try:
            outcome = future.result()
            commit_started_at = time.monotonic()
            try:
                _commit_job_evaluation(outcome, llm, language)
            finally:
                metrics.record_commit(time.monotonic() - commit_started_at)
        except Exception as exc:
            metrics.record_completion(failed=True)
            completed.append((task, exc))
        else:
            metrics.record_completion(failed=False)
            completed.append((task, None))
    return completed


def evaluate_cached_jobs(
    jobs: list[JobResult],
    *,
    profile: CVProfile,
    llm,
    cv_hash: str,
    language: str = "zh",
    workers: int = 1,
) -> tuple[int, int]:
    """为已缓存职位补算 JD profile 与 CV 匹配，返回 (成功, 失败) 条数。

    ``assess`` 命令用此入口补全 ``job_matches``。写入 legacy ``assessment`` 列是不够的：
    待评估判定看的是当前 cv_hash 下有无匹配结果，只写旧列会让同一批职位被反复捞出。
    """
    if not jobs:
        return 0, 0
    tasks = [
        JobEvaluationTask(
            key=job.dedup_key,
            job=job,
            full_jd=job.description_snippet,
            kind="assess",
            source=job.sources[0] if job.sources else "unknown",
        )
        for job in jobs
    ]
    metrics = AssessmentConcurrencyMetrics(workers=workers)
    results = _evaluate_jobs(
        tasks,
        profile=profile,
        llm=llm,
        cv_hash=cv_hash,
        language=language,
        executor=None,
        workers=workers,
        metrics=metrics,
    )
    failed = 0
    for task, error in results:
        if error is not None:
            failed += 1
            logger.warning("Assessment failed for %s: %s", task.key, error)
    return len(results) - failed, failed


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
    *,
    evaluation_executor: Executor | None = None,
    assessment_workers: int = 1,
    concurrency_metrics: AssessmentConcurrencyMetrics | None = None,
) -> tuple[list[str], int, int]:
    if assessment_workers < 1:
        raise ValueError("assessment_workers must be at least 1")
    metrics = concurrency_metrics or AssessmentConcurrencyMetrics(workers=assessment_workers)
    def _is_visible_job(job_obj) -> bool:
        return bool(job_obj is not None and job_obj.is_effectively_relevant)

    has_cv = bool(profile.summary and profile.skills)
    keys: list[str] = []
    llm_rejected = 0
    new_saved = 0

    if pf.immediate_keys and has_cv and llm:
        cb(f"Checking explainable scores for {len(pf.immediate_keys)} cached jobs...")

    immediate_tasks: list[JobEvaluationTask] = []
    for key in pf.immediate_keys:
        job_obj = cache.get_job(key, language=language)
        if job_obj is not None and has_cv and llm:
            immediate_tasks.append(
                JobEvaluationTask(
                    key=key,
                    job=job_obj,
                    full_jd=job_obj.description_snippet,
                    kind="immediate",
                    source=job_obj.sources[0] if job_obj.sources else "unknown",
                )
            )
            continue
        if not _is_visible_job(job_obj):
            logger.debug("Skip cached result after final relevance check: %s", key)
            continue
        keys.append(key)
        if on_job:
            on_job(key)

    for task, error in _evaluate_jobs(
        immediate_tasks,
        profile=profile,
        llm=llm,
        cv_hash=cv_hash,
        language=language,
        executor=evaluation_executor,
        workers=assessment_workers,
        metrics=metrics,
    ):
        if error is not None:
            logger.warning("JD profile extraction/matching failed for cached job %s: %s", task.key, error)
        final_job = cache.get_job(task.key, language=language) or task.job
        if not _is_visible_job(final_job):
            logger.debug("Skip cached result after final relevance check: %s", task.key)
            continue
        keys.append(task.key)
        if on_job:
            on_job(task.key)

    if pf.patch_pending and has_cv and llm:
        cb(f"Re-assessing {len(pf.patch_pending)} cached jobs...")
        patch_inputs = [(cached_job.title, cached_job.description_snippet) for cached_job, _ in pf.patch_pending]
        patch_assessments = batch_assess_jds(patch_inputs, profile, llm, language=language)
        patch_tasks: list[JobEvaluationTask] = []
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
                cache.save_job_relevance_rejection(
                    job_id=cached_job.dedup_key,
                    cv_hash=cv_hash,
                    description=cached_job.description_snippet,
                    reason=assessment.reason,
                    score=assessment.score,
                    model_name=f"{llm.provider}/{llm.model}",
                    prompt_version=jd_assessment_prompt_version(language),
                )
            write_cache(
                {
                    "title": cached_job.title,
                    "company": cached_job.company,
                    "location": cached_job.location,
                    "url": cached_job.url,
                    "description_snippet": cached_job.description_snippet,
                    "sources": cached_job.sources,
                    "raw_sources": cached_job.raw_sources,
                    "date_posted": cached_job.date_posted,
                    "expires_at": cached_job.expires_at,
                    "is_complete": cached_job.is_complete,
                    "coarse_filter": cached_job.coarse_filter,
                }
            )
            if assessment.relevant:
                refreshed_job = cache.get_job(cached_job.dedup_key) or cached_job
                patch_tasks.append(
                    JobEvaluationTask(
                        key=cached_job.dedup_key,
                        job=refreshed_job,
                        full_jd=refreshed_job.description_snippet,
                        kind="patch",
                        source=refreshed_job.sources[0] if refreshed_job.sources else "unknown",
                    )
                )

        for task, error in _evaluate_jobs(
            patch_tasks,
            profile=profile,
            llm=llm,
            cv_hash=cv_hash,
            language=language,
            executor=evaluation_executor,
            workers=assessment_workers,
            metrics=metrics,
        ):
            if error is not None:
                logger.warning("JD profile extraction failed for cached job %s: %s", task.key, error)
            final_job = cache.get_job(task.key, language=language) or task.job
            if not _is_visible_job(final_job):
                cb(f"Skip (final match): {task.job.title[:50]}")
                logger.info("Final match filtered cached job: %s", task.job.title)
                cache.record_filter_event(
                    run_id=run_id,
                    stage="final_match",
                    title=task.job.title,
                    company=task.job.company,
                    location=task.job.location,
                    url=task.job.url,
                    reason="match recommendation=skip",
                    details={"cached": True},
                )
                continue
            keys.append(task.key)
            if on_job:
                on_job(task.key)

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

        pending_tasks: list[JobEvaluationTask] = []
        pending_without_evaluation: list[JobEvaluationTask] = []
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
                    "sources": [entry["source"] for entry in raw_sources],
                    "raw_sources": raw_sources,
                }
            )
            if not assessment.relevant and cv_hash and llm is not None:
                cache.save_job_relevance_rejection(
                    job_id=key,
                    cv_hash=cv_hash,
                    description=content,
                    reason=assessment.reason,
                    score=assessment.score,
                    model_name=f"{llm.provider}/{llm.model}",
                    prompt_version=jd_assessment_prompt_version(language),
                )
            if assessment.relevant:
                summary_job = JobResult(
                    title=title,
                    company=job.get("company", ""),
                    location=job.get("location", ""),
                    url=job.get("url", ""),
                    description_snippet=content,
                    sources=[entry["source"] for entry in raw_sources],
                    raw_sources=raw_sources,
                    date_posted=job.get("date_posted", ""),
                    expires_at=expires_at,
                    is_complete=job.get("is_complete", True),
                    coarse_filter=job.get("coarse_filter"),
                )
                if llm is not None:
                    pending_tasks.append(
                        JobEvaluationTask(
                            key=key,
                            job=summary_job,
                            full_jd=summary_job.description_snippet,
                            kind="pending",
                            source=job_source,
                        )
                    )
                else:
                    pending_without_evaluation.append(
                        JobEvaluationTask(
                            key=key,
                            job=summary_job,
                            full_jd=summary_job.description_snippet,
                            kind="pending",
                            source=job_source,
                        )
                    )

        evaluated = _evaluate_jobs(
            pending_tasks,
            profile=profile,
            llm=llm,
            cv_hash=cv_hash,
            language=language,
            executor=evaluation_executor,
            workers=assessment_workers,
            metrics=metrics,
        )
        evaluated.extend((task, None) for task in pending_without_evaluation)
        for task, error in evaluated:
            if error is not None:
                logger.warning("JD profile extraction/matching failed for %s: %s", task.key, error)
            final_job = task.job
            if not _is_visible_job(final_job):
                cb(f"Skip (final match): {task.job.title[:50]}")
                logger.info("Final match filtered job: %s", task.job.title)
                cache.record_filter_event(
                    run_id=run_id,
                    stage="final_match",
                    title=task.job.title,
                    company=task.job.company,
                    location=task.job.location,
                    source=task.source,
                    url=task.job.url,
                    reason="match recommendation=skip",
                    details={"cached": False},
                )
                continue
            source_stats = pf.source_stats.setdefault(task.source, {key: 0 for key in SOURCE_STATS_KEYS})
            source_stats["saved"] += 1
            new_saved += 1
            keys.append(task.key)
            if on_job:
                on_job(task.key)
            cb(f"Saved: {task.job.title} @ {task.job.company or '?'} [{task.source}]")

    total_assessed = len(pf.pending) + len(pf.patch_pending)
    if total_assessed >= 5 and llm_rejected / total_assessed >= 0.9:
        logger.warning(
            "High LLM rejection rate: %.0f%% (%d/%d rejected) — check CV profile or prompt",
            llm_rejected / total_assessed * 100,
            llm_rejected,
            total_assessed,
        )

    return keys, llm_rejected, new_saved
