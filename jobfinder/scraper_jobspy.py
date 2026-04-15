"""
JobSpy 抓取接口，输出与现有 SCRAPER_REGISTRY 相同的 list[dict] 格式。

包含：
  - scrape_indeed_jobspy        单关键词抓取
  - scrape_indeed_jobspy_multi  多role串行抓取（含限速）
  - _filter_cards_by_llm        LLM 批量标题评分过滤
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Callable  # noqa: F401

from pydantic import BaseModel

from jobfinder.logger import get_logger

if TYPE_CHECKING:
    from jobfinder.schemas import CVProfile

logger = get_logger(__name__)


# ── LLM 标题批量过滤 ──────────────────────────────────────────────────────────

class _CardScore(BaseModel):
    id: int
    score: float  # 0.0 ~ 1.0


class _CardScoreList(BaseModel):
    scores: list[_CardScore]


def _filter_cards_by_llm(
    cards_meta: list[dict],
    cv_profile: "CVProfile",
    provider: str,
    model: str,
    threshold: float = 0.6,
) -> set[int]:
    """
    单次 LLM 调用对所有卡片批量打分，返回 score >= threshold 的 id 集合。
    失败时保留全部（降级）。
    """
    from jobfinder.llm_backend import complete_structured

    roles_str  = ", ".join(cv_profile.preferred_roles[:10])
    skills_str = ", ".join(cv_profile.skills[:15])
    cards_text = "\n".join(
        f"id={c['id']} | title={c['title']} | company={c['company']} | location={c['location']}"
        for c in cards_meta
    )

    prompt = f"""你是招聘筛选助手。根据候选人信息，对以下每个职位卡片打分（0.0~1.0）。

候选人信息：
- 目标职位：{roles_str}
- 技能：{skills_str}
- 资历：{cv_profile.seniority}
- 摘要：{cv_profile.summary}

职位卡片列表：
{cards_text}

评分标准（以候选人目标职位和技能为基准）：
- 1.0：标题与目标职位高度吻合
- 0.8：标题方向一致
- 0.6：可能相关，值得查看详情
- 0.4：相关性较低
- 0.0：与候选人专业背景明显无关

只返回 JSON，格式：{{"scores": [{{"id": 0, "score": 0.9}}, ...]}}"""

    try:
        result = complete_structured(
            prompt=prompt,
            response_schema=_CardScoreList,
            provider=provider,
            model=model,
            system="你是招聘筛选助手，只返回 JSON。",
            _step="",
        )
        passed = {s.id for s in result.scores if s.score >= threshold}
        logger.debug(
            "LLM 卡片过滤：%d/%d 通过（threshold=%.1f）",
            len(passed), len(cards_meta), threshold,
        )
        return passed
    except Exception as e:
        logger.warning("Card LLM filter failed, keeping all: %s", e)
        return {c["id"] for c in cards_meta}


def _html_to_text(html: str) -> str:
    """BeautifulSoup 剥离 HTML 标签，保留段落换行。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    # 在块级元素前后插入换行
    for tag in soup.find_all(["p", "li", "br", "h1", "h2", "h3", "h4"]):
        tag.insert_before("\n")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text()).strip()


# location 清洗：'DUBLIN 2, D, IE' → 'Dublin 2, Ireland'
_COUNTRY_CODE = re.compile(r",\s*[A-Z]{2}\s*$")
_STATE_CODE    = re.compile(r",\s*[A-Z]\s*(?=,)")

def _clean_location(raw: str) -> str:
    if not raw:
        return ""
    loc = _COUNTRY_CODE.sub(", Ireland", raw)
    loc = _STATE_CODE.sub("", loc)
    return loc.title().strip()


def scrape_indeed_jobspy(
    keyword: str,
    limit: int = 20,
    country: str = "ireland",
    hours_old: int = 168,
) -> list[dict]:
    """
    用 JobSpy 抓取 Indeed，返回与现有 scraper 相同格式的 list[dict]。

    输出字段：
        title, company, location, url, apply_url,
        source, is_complete, description_snippet,
        date_posted, is_remote
    """
    try:
        import jobspy
    except ImportError:
        logger.error("python-jobspy not installed, run: uv add python-jobspy")
        return []

    logger.info("JobSpy indeed [%s @ %s] starting scrape (limit=%d)", keyword, country, limit)
    try:
        df = jobspy.scrape_jobs(
            site_name=["indeed"],
            search_term=keyword,
            location=country.title(),
            country_indeed=country,
            results_wanted=limit,
            hours_old=hours_old,
            description_format="html",
            verbose=0,
        )
    except Exception as e:
        logger.warning("JobSpy indeed 抓取失败：%s", e)
        return []

    if df is None or df.empty:
        logger.info("JobSpy indeed [%s] → 0 条", keyword)
        return []

    results = []
    for _, row in df.iterrows():
        title   = str(row.get("title")   or "").strip()
        job_url = str(row.get("job_url") or "").strip()
        if not title or not job_url:
            continue

        raw_desc    = str(row.get("description") or "").strip()
        description = _html_to_text(raw_desc) if raw_desc else ""
        company = str(row.get("company") or "").strip()
        if not company or company.lower() == "nan":
            continue
        results.append({
            "title":               title,
            "company":             company,
            "location":            _clean_location(str(row.get("location") or "")),
            "url":                 job_url,
            "apply_url":           str(row.get("job_url_direct")  or job_url).strip(),
            "source":              "indeed.ie",
            "is_complete":         bool(description),
            "description_snippet": description[:15000],
            # 现有实现没有的额外字段
            "date_posted":         str(row.get("date_posted") or ""),
            "is_remote":           bool(row.get("is_remote")),
        })

    logger.info("JobSpy indeed [%s] → %d 条", keyword, len(results))
    return results


_INTER_ROLE_DELAY = 2.0  # 每个 role 之间的间隔（秒），避免触发 Indeed 限流


def scrape_indeed_jobspy_multi(
    roles: list[str],
    limit_per_role: int = 200,
    country: str = "ireland",
    cb: Callable[[str], None] | None = None,
) -> list[dict]:
    """
    多 role 串行抓取（含限速），去重后返回。

    串行而非并发，避免 Indeed 因短时高频请求触发限流。
    每个 role 之间等待 _INTER_ROLE_DELAY 秒。
    """
    if cb:
        cb(f"JobSpy scraping (indeed.ie): {roles}")

    seen: set[str] = set()
    jobs: list[dict] = []

    for i, role in enumerate(roles):
        if i > 0:
            time.sleep(_INTER_ROLE_DELAY)
        batch = scrape_indeed_jobspy(role, limit_per_role, country)
        for job in batch:
            url = job.get("url", "")
            if url and url not in seen:
                seen.add(url)
                jobs.append(job)
        if cb:
            cb(f"  [{i+1}/{len(roles)}] {role!r} → {len(batch)} results")

    logger.info("JobSpy indeed 全部 role 完成：%d 条（URL 去重后）", len(jobs))
    if cb:
        cb(f"JobSpy done: {len(jobs)} jobs (after dedup)")
    return jobs
