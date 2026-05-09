from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from jobradar import cache
from jobradar.filters import infer_title_seniority, is_title_seniority_ok
from jobradar.logger import get_logger
from jobradar.schemas import CVProfile, is_closed_posting, make_dedup_key
from jobradar.tools import record_failed_url

logger = get_logger(__name__)

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
    r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}"
    r"|\d{1,2}\s+\w+\s+\d{4}"
    r"|\w+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

SOURCE_STATS_KEYS = (
    "in",
    "dup",
    "skip_seniority",
    "skip_irrelevant",
    "skip_exp",
    "cache_hit",
    "no_desc",
    "closed",
    "llm_rejected",
    "saved",
)


def extract_max_years_requirement(snippet: str) -> int | None:
    matches = re.findall(
        r"(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience|exp\b)",
        snippet,
        re.IGNORECASE,
    )
    years = [int(item) for item in matches if item.isdigit()]
    return max(years) if years else None


def collect_all_sources(jobs: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for job in jobs:
        dedup_key = make_dedup_key((job.get("company") or "").strip(), (job.get("title") or "").strip())
        entry = {
            "source": job.get("source") or "unknown",
            "url": job.get("url") or "",
            "date_posted": job.get("date_posted") or "",
        }
        if dedup_key not in result:
            result[dedup_key] = []
        if not any(existing["source"] == entry["source"] for existing in result[dedup_key]):
            result[dedup_key].append(entry)
    return result


@dataclass
class PrefilterResult:
    immediate_keys: list[str] = field(default_factory=list)
    pending: list[tuple[dict, str, str | None]] = field(default_factory=list)
    patch_pending: list[tuple[object, str]] = field(default_factory=list)
    total: int = 0
    skip_dup: int = 0
    skip_seniority: int = 0
    skip_irrelevant: int = 0
    title_relevance_in: int = 0
    title_relevance_rejected: int = 0
    cache_hit: int = 0
    cache_patch: int = 0
    skip_no_desc: int = 0
    skip_closed: int = 0
    skip_exp: int = 0
    skip_skill: int = 0
    source_stats: dict[str, dict[str, int]] = field(default_factory=dict)


def prefilter_jobs(
    jobs: list[dict],
    seen_urls: set[str],
    cb: Callable[[str], None],
    profile: CVProfile,
    language: str = "zh",
    run_id: str = "",
) -> PrefilterResult:
    del language
    result = PrefilterResult()
    seen_dedup_keys: set[str] = set()
    title_candidates: list[tuple[dict, str, str, str, str, dict[str, int]]] = []

    for job in jobs:
        result.total += 1
        source = job.get("source") or "unknown"
        source_stats = result.source_stats.setdefault(source, {key: 0 for key in SOURCE_STATS_KEYS})
        source_stats["in"] += 1

        url = job.get("url", "")
        if not url or url in seen_urls:
            source_stats["dup"] += 1
            result.skip_dup += 1
            continue
        seen_urls.add(url)

        title = (job.get("title") or "").strip()
        if not title:
            source_stats["dup"] += 1
            result.skip_dup += 1
            continue
        company = (job.get("company") or "").strip()

        if not is_title_seniority_ok(title, profile):
            inferred = infer_title_seniority(title)
            logger.debug("Skip (title seniority gate): %s | inferred=%s | profile=%s", title, inferred, profile.seniority_display)
            cb(f"Skip (title seniority): {title[:60]} — inferred={inferred or 'unknown'} blocked_for={profile.seniority_display}")
            cache.record_filter_event(
                run_id=run_id,
                stage="title_seniority",
                title=title,
                company=company,
                location=job.get("location", ""),
                source=source,
                url=url,
                reason=f"inferred={inferred or 'unknown'} blocked_for={profile.seniority_display}",
                details={"profile_seniority": profile.seniority_display, "inferred_seniority": inferred or "unknown"},
            )
            source_stats["skip_seniority"] += 1
            result.skip_seniority += 1
            continue

        dedup_key = make_dedup_key(company, title)
        if dedup_key in seen_dedup_keys:
            logger.debug("Skip (cross-source dedup): %s @ %s", title, company)
            source_stats["dup"] += 1
            result.skip_dup += 1
            continue
        seen_dedup_keys.add(dedup_key)

        cached_job = cache.get_job_by_url(url)
        if cached_job is not None and not cached_job.is_expired:
            if cached_job.assessment is not None:
                if not cached_job.assessment.is_relevant:
                    logger.debug("URL cache hit (rejected), skip: %s", title)
                    source_stats["cache_hit"] += 1
                    result.cache_hit += 1
                    continue
                logger.debug("URL cache hit, skip fetch+LLM: %s", title)
                result.immediate_keys.append(cached_job.dedup_key)
                source_stats["cache_hit"] += 1
                result.cache_hit += 1
                continue
            logger.debug("URL cache hit, pending LLM re-assess: %s", title)
            result.patch_pending.append((cached_job, cached_job.description_snippet))
            result.cache_patch += 1
            continue

        title_candidates.append((job, title, company, url, source, source_stats))

    profile_years = profile.years_of_experience or 0
    for job, title, company, url, source, source_stats in title_candidates:
        content = (job.get("description_snippet") or "").strip()
        if not content:
            logger.debug("Skip (no description): %s", title)
            cb(f"Skip (no description): {title[:50]}")
            cache.record_filter_event(
                run_id=run_id,
                stage="no_description",
                title=title,
                company=company,
                location=job.get("location", ""),
                source=source,
                url=url,
                reason="missing description_snippet",
            )
            source_stats["no_desc"] += 1
            result.skip_no_desc += 1
            continue

        if is_closed_posting(content):
            record_failed_url(url, "posting_closed")
            cb(f"Skip (posting closed): {url[:70]}")
            logger.info("Posting closed: %s", url)
            cache.record_filter_event(
                run_id=run_id,
                stage="posting_closed",
                title=title,
                company=company,
                location=job.get("location", ""),
                source=source,
                url=url,
                reason="posting_closed",
            )
            source_stats["closed"] += 1
            result.skip_closed += 1
            continue

        years_required = extract_max_years_requirement(content[:2000])
        if years_required is not None and years_required - profile_years >= 4:
            reason = f"requires {years_required}+ years; candidate has ~{profile_years:g} years"
            logger.debug("Skip (experience gap): %s | %s", title, reason)
            cb(f"Skip (experience gap): {title[:60]} — {reason}")
            cache.record_filter_event(
                run_id=run_id,
                stage="experience_gap",
                title=title,
                company=company,
                location=job.get("location", ""),
                source=source,
                url=url,
                reason=reason,
                details={"years_required": years_required, "profile_years": profile_years},
            )
            source_stats["skip_exp"] += 1
            result.skip_exp += 1
            continue

        expires_at = job.get("expires_at")
        if not expires_at:
            match = _DEADLINE_PATTERN.search(content[:1000])
            if match:
                expires_at = match.group(1).strip()

        result.pending.append((job, content, expires_at))

    return result
