"""Job Search：直接抓取目标站点，不依赖外部搜索 API。

流程：
  scrapers.SCRAPER_REGISTRY 中所有站点并发抓取
  → 标题相关性过滤 → 资历过滤 → Fetch 完整 JD → LLM 匹配评估 → 写缓存
"""
from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel

from jobfinder import cache
from jobfinder.filters import is_seniority_ok
from jobfinder.logger import get_logger
from jobfinder.llm_backend import DEFAULT_MODELS, LLMConfig, Provider, complete_structured
from jobfinder.schemas import CVProfile, JobAssessment, SearchSession, is_closed_posting
from jobfinder.scrapers_jobspy import scrape_sources
from jobfinder.tools import (
    fetch_page,
    record_failed_url,
    write_cache,
)

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
    # 兼容旧参数，优先使用 llm
    provider: Provider = "claude",
    model: str | None = None,
) -> list[str]:
    """
    抓取所有注册站点，过滤并写缓存，返回本次写入的 dedup_key 列表。
    """
    effective_llm = llm or LLMConfig(
        provider=provider,
        model=model or DEFAULT_MODELS.get(provider, ""),
    )

    def _cb(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # ── Session 缓存检查 ───────────────────────────────────────────────────────
    session = SearchSession(
        roles=profile.preferred_roles,
        location=location,
        seniority=profile.seniority,
        search_language=profile.search_language,
    )
    if not force_refresh:
        cached = cache.get_session(session.session_key)
        if cached is not None:
            logger.info("Cache session hit (%d results)", len(cached.job_dedup_keys))
            _cb(f"Cached session hit — skipping scrape ({len(cached.job_dedup_keys)} jobs)")
            if on_job:
                for k in cached.job_dedup_keys:
                    on_job(k)
            return cached.job_dedup_keys

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
        )
        logger.info("Scraped %d jobs, starting filter & LLM assessment...", len(scraped))
        role_keywords = _build_role_keywords(profile.preferred_roles)
        keys = _write_scraped(  # noqa: E501
            scraped, seen_urls, _cb,
            role_keywords=role_keywords,
            profile=profile,
            llm=effective_llm,
            on_job=on_job,
            language=language,
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
    return collected_keys


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────

# 上下文限定的截止日期正则：必须有"截止语义词 + 日期"的组合，避免误匹配其他数字
_DEADLINE_PATTERN = re.compile(
    r"(?:closing\s+date|apply\s+by|deadline|applications?\s+close[sd]?|closes?\s+on)"
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


def _write_scraped(
    jobs: list[dict],
    seen_urls: set[str],
    cb: Callable[[str], None],
    role_keywords: set[str] | None = None,
    profile: CVProfile | None = None,
    llm: LLMConfig | None = None,
    on_job: Callable[[str], None] | None = None,
    language: str = "zh",
    # 兼容旧参数
    seniority: str = "",
    max_years: int = 99,
    cv_skills: list[str] | None = None,
    cv_summary: str = "",
) -> list[str]:
    """
    将浏览器直接抓取的结构化职位写入缓存。
    两阶段处理：
      阶段一  pre-filter（年资/相关性/缓存命中/fetch/关闭/经验/技能）→ 收集待评估列表
      阶段二  批量 LLM 评估（每批 _BATCH_SIZE 条，system prompt 只发一次）→ 写缓存
    """
    # 统一从 profile 取值，兼容旧参数传入
    _seniority = profile.seniority if profile else seniority
    _max_years = profile.years_of_experience if profile else max_years
    _cv_skills = profile.skills if profile else (cv_skills or [])
    _cv_summary = profile.summary if profile else cv_summary

    # 构建技能关键词集合（用于快速预筛）
    skill_keywords: set[str] = set()
    for s in _cv_skills:
        skill_keywords.update(w.lower() for w in re.split(r"[\s/\-,]+", s) if len(w) > 1)

    # ── 阶段一：pre-filter ────────────────────────────────────────────────────
    # immediate_keys: URL 缓存命中且已有 assessment，直接复用
    immediate_keys: list[str] = []
    # pending: (job_dict, content, expires_at)，通过所有 pre-filter 待 LLM 评估
    pending: list[tuple[dict, str, str | None]] = []
    # patch_keys: URL 缓存命中但缺少 assessment，需补跑 LLM，暂存 cached_job
    patch_pending: list[tuple[object, str]] = []  # (cached_job, content)

    # 过滤漏斗计数器
    _total = 0
    _skip_dup = 0
    _skip_seniority = 0
    _skip_irrelevant = 0
    _cache_hit = 0
    _cache_patch = 0
    _skip_fetch_fail = 0
    _skip_closed = 0
    _skip_exp = 0
    _skip_skill = 0

    for job in jobs:
        _total += 1
        url = job.get("url", "")
        if not url or url in seen_urls:
            _skip_dup += 1
            continue
        seen_urls.add(url)

        title = (job.get("title") or "").strip()
        if not title:
            _skip_dup += 1
            continue

        # 1. 年资过滤（零成本，title 即可判断）
        if _seniority and not is_seniority_ok(title, _seniority):
            logger.debug("Skip (seniority mismatch): %s", title)
            cb(f"Skip (seniority mismatch): {title[:60]}")
            _skip_seniority += 1
            continue

        # 2. 相关性过滤（标题关键词）
        if role_keywords and not _is_title_relevant(title, role_keywords):
            logger.debug("Skip (irrelevant title): %s", title)
            cb(f"Skip (irrelevant title): {title[:60]}")
            _skip_irrelevant += 1
            continue

        # 3. URL 缓存命中检查
        cached_job = cache.get_job_by_url(url)
        if cached_job is not None and not cached_job.is_expired:
            if cached_job.assessment is not None or not (_cv_summary and _cv_skills):
                logger.debug("URL cache hit, skip fetch+LLM: %s", title)
                immediate_keys.append(cached_job.dedup_key)
                _cache_hit += 1
                continue
            else:
                logger.debug("URL cache hit, pending LLM re-assess: %s", title)
                patch_pending.append((cached_job, cached_job.description_snippet))
                _cache_patch += 1
                continue

        # 4. 获取 JD 内容
        prefetched = (job.get("description_snippet") or "").strip()
        if len(prefetched) > 200:
            content = prefetched
            logger.debug("Using pre-fetched JD (%d chars): %s", len(content), title)
        else:
            cb(f"Fetching JD: {title[:50]}")
            content = fetch_page(url)
            if not content:
                record_failed_url(url, "fetch_failed")
                logger.warning("Fetch failed: %s", url)
                _skip_fetch_fail += 1
                continue

        # 5. 关闭检测
        if is_closed_posting(content):
            record_failed_url(url, "posting_closed")
            cb(f"Skip (posting closed): {url[:70]}")
            logger.info("Posting closed: %s", url)
            _skip_closed += 1
            continue

        # 6. 经验年限检查
        if _over_experience_limit(content, _max_years):
            cb(f"Skip (experience requirement too high): {title[:60]}")
            logger.debug("Experience requirement too high: %s", title)
            _skip_exp += 1
            continue

        # 7. 技能关键词预筛（至少匹配 1 个）
        if skill_keywords:
            if not any(s in content.lower() for s in skill_keywords):
                cb(f"Skip (skill mismatch): {title[:60]}")
                logger.debug("Skill mismatch: %s", title)
                _skip_skill += 1
                continue

        # 截止日期提取（scraper 优先，兜底扫文本）
        expires_at = job.get("expires_at")
        if not expires_at:
            m = _DEADLINE_PATTERN.search(content[:1000])
            if m:
                expires_at = m.group(1).strip()

        pending.append((job, content, expires_at))

    # ── 阶段二：批量 LLM 评估 ────────────────────────────────────────────────
    logger.info(
        "Pre-filter done: cache hit %d | pending LLM %d (new %d + re-assess %d)",
        _cache_hit, len(pending) + len(patch_pending), len(pending), len(patch_pending),
    )
    keys: list[str] = []
    # 缓存命中的职位也触发 on_job（前端可立即展示）
    for k in immediate_keys:
        keys.append(k)
        if on_job:
            on_job(k)

    # 2a. patch：URL 缓存命中但缺 assessment 的条目
    _llm_rejected = 0
    if patch_pending and _cv_summary and _cv_skills and profile and llm:
        cb(f"Re-assessing {len(patch_pending)} cached jobs...")
        patch_inputs = [(cj.title, cj.description_snippet) for cj, _ in patch_pending]
        patch_assessments = _batch_assess_jds(patch_inputs, profile, llm, language=language)
        for (cached_job, _), assessment in zip(patch_pending, patch_assessments):
            if not assessment.relevant:
                cb(f"Skip (not relevant): {cached_job.title[:50]} — {assessment.reason}")
                logger.info("LLM re-assess rejected: %s | %s", cached_job.title, assessment.reason)
                _llm_rejected += 1
                continue
            key = write_cache({
                "title": cached_job.title,
                "company": cached_job.company,
                "location": cached_job.location,
                "url": cached_job.url,
                "description_snippet": cached_job.description_snippet,
                "expires_at": cached_job.expires_at,
                "is_complete": cached_job.is_complete,
                "assessment": assessment.to_job_assessment(),
            })
            keys.append(key)
            if on_job:
                on_job(key)

    # 2b. 新抓取的职位批量评估
    if pending:
        if _cv_summary and _cv_skills and profile and llm:
            cb(f"LLM assessing {len(pending)} jobs (batch size {_BATCH_SIZE})...")
            batch_inputs = [(job.get("title", ""), content) for job, content, _ in pending]
            assessments = _batch_assess_jds(batch_inputs, profile, llm, language=language)
        else:
            # 无 CV 信息时不评估，全部保留
            assessments = [
                _JDAssessment(
                    relevant=True, reason="无 CV 信息，默认保留",
                    score=0, strengths=[], weaknesses=[], matched_keywords=[],
                )
                for _ in pending
            ]

        for (job, content, expires_at), assessment in zip(pending, assessments):
            title = (job.get("title") or "").strip()
            if not assessment.relevant:
                cb(f"Skip (not relevant): {title[:50]} — {assessment.reason}")
                logger.info("LLM assess rejected: %s | %s", title, assessment.reason)
                _llm_rejected += 1
                continue
            logger.debug("LLM assess matched: %s | score=%d", title, assessment.score)

            job_assessment = assessment.to_job_assessment() if (_cv_summary and _cv_skills) else None
            key = write_cache({
                "title": title,
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "url": job.get("url", ""),
                "description_snippet": content,
                "expires_at": expires_at,
                "is_complete": job.get("is_complete", True),
                "assessment": job_assessment,
            })
            keys.append(key)
            if on_job:
                on_job(key)
            cb(f"Saved: {title} @ {job.get('company', '?')} [{job.get('source', '')}]")

    # ── 汇总日志 ─────────────────────────────────────────────────────────────
    _saved = len(keys)
    logger.info(
        "Filter funnel | input=%d dup_skip=%d seniority=%d irrelevant=%d cache_hit=%d cache_patch=%d "
        "fetch_fail=%d closed=%d exp_limit=%d skill_mismatch=%d llm_in=%d llm_rejected=%d saved=%d",
        _total, _skip_dup, _skip_seniority, _skip_irrelevant,
        _cache_hit, _cache_patch, _skip_fetch_fail, _skip_closed,
        _skip_exp, _skip_skill, len(pending) + len(patch_pending),
        _llm_rejected, _saved,
    )
    cb(
        f"Summary: {_total} in → seniority {_skip_seniority} | irrelevant {_skip_irrelevant} | "
        f"cache hit {_cache_hit} | fetch fail {_skip_fetch_fail} | closed {_skip_closed} | "
        f"exp limit {_skip_exp} | skill mismatch {_skip_skill} | LLM rejected {_llm_rejected} → saved {_saved}"
    )

    return keys


class _JDAssessment(BaseModel):
    relevant: bool
    reason: str                  # 一句话说明原因（用于日志/过滤）
    score: int                   # CV 与 JD 整体匹配分 0~10
    strengths: list[str]         # CV 相对于该 JD 的优势（2~4 条）
    weaknesses: list[str]        # CV 相对于该 JD 的劣势（2~4 条）
    matched_keywords: list[str]  # CV 与 JD 重叠的具体技能/关键词

    def to_job_assessment(self) -> JobAssessment:
        """转换为持久化用的 JobAssessment（丢弃 relevant/reason 过滤字段）。"""
        return JobAssessment(
            score=self.score,
            strengths=self.strengths,
            weaknesses=self.weaknesses,
            matched_keywords=self.matched_keywords,
        )


class _BatchAssessmentResult(BaseModel):
    results: list[_JDAssessment]


_BATCH_SIZE = 8   # 每批 JD 数量，兼顾 context 长度与 token 节省


_LANGUAGE_NAMES = {"zh": "中文", "en": "English", "es": "Español"}

def _batch_assess_jds(
    jobs: list[tuple[str, str]],   # (title, jd_content)
    profile: CVProfile,
    llm: LLMConfig,
    language: str = "zh",
) -> list[_JDAssessment]:
    """
    批量评估 JD 列表，返回与输入等长的评估结果列表。
    每批最多 _BATCH_SIZE 条，system prompt 只发一次，节省约 60% token。
    任意一批失败时对应条目默认 relevant=True（保守保留）。
    """
    if not jobs:
        return []

    skills_str = ", ".join(profile.skills[:20])
    leniency_note = ""
    if profile.seniority in ("new_grad", "intern", "junior"):
        leniency_note = (
            "\n注意：relevant 判断应偏宽松——只要职位方向与候选人专业有实质关联即可标记为 relevant。"
            "但 strengths/weaknesses 必须客观如实，不受此宽松原则影响。"
        )

    lang_name = _LANGUAGE_NAMES.get(language, "中文")
    system = f"你是招聘筛选助手，只返回 JSON，不要额外解释。无论职位描述使用何种语言，所有文字字段必须用 {lang_name} 输出。"
    results: list[_JDAssessment] = []

    for batch_start in range(0, len(jobs), _BATCH_SIZE):
        batch = jobs[batch_start: batch_start + _BATCH_SIZE]

        jd_blocks = []
        for idx, (title, content) in enumerate(batch, 1):
            jd_blocks.append(f"[{idx}] 职位：{title}\n{content[:4000]}")
        jd_section = "\n\n---\n\n".join(jd_blocks)

        prompt = f"""【输出语言：{lang_name}，所有文字字段必须用 {lang_name} 撰写】

根据候选人信息，批量评估以下 {len(batch)} 个职位与候选人的匹配程度。

候选人摘要：{profile.summary}
候选人技能：{skills_str}
候选人资历：{profile.seniority or "未知"}，实际工作年限：{profile.years_of_experience} 年{leniency_note}

判断标准：
- 职位要求的核心技能与候选人技能有实质重叠
- 职位要求的经验年限在候选人能力范围内
- 职位类型与候选人目标方向吻合

职位列表（共 {len(batch)} 个，按编号 [1]~[{len(batch)}] 排列）：

{jd_section}

请按编号顺序，在 results 数组中返回每个职位的评估，字段：
- relevant：bool，职位是否值得投递
- reason：一句话说明 relevant 判断的理由（用 {lang_name}）
- score：整数 0~10，综合匹配分
- strengths：list[str]，候选人申请该职位的真实优势，0~5 条；若无实质优势则返回空列表（用 {lang_name}）
- weaknesses：list[str]，候选人申请该职位的真实劣势，0~5 条；若无实质劣势则返回空列表；
  若 JD 明确要求的工作年限超过候选人实际年限，必须在此列出（用 {lang_name}）
- matched_keywords：list[str]，CV 技能与 JD 要求中重叠的具体关键词（3~8 个，保留原始技术词汇）

results 数组长度必须等于 {len(batch)}，顺序与编号一一对应。"""

        _default = _JDAssessment(
            relevant=True, reason="评估失败，默认保留",
            score=0, strengths=[], weaknesses=[], matched_keywords=[],
        )
        try:
            batch_result = complete_structured(
                prompt=prompt,
                response_schema=_BatchAssessmentResult,
                provider=llm.provider,
                model=llm.model,
                system=system,
                _step="JD 批量评估",
            )
            assessments = batch_result.results
            while len(assessments) < len(batch):
                assessments.append(_default)
            results.extend(assessments[: len(batch)])
            logger.info("Batch assess: batch %d, %d jobs done", batch_start // _BATCH_SIZE + 1, len(batch))
        except Exception as e:
            logger.warning("Batch JD assess failed (batch %d), defaulting all to keep: %s", batch_start // _BATCH_SIZE + 1, e)
            results.extend([_default for _ in batch])

    return results


def _over_experience_limit(snippet: str, max_years: int) -> bool:
    matches = re.findall(
        r"(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience|exp\b)",
        snippet,
        re.IGNORECASE,
    )
    return any(int(m) > max_years for m in matches)


