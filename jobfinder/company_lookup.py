"""公司信息查询 skill：Jina Reader 抓取官网 + LLM 分析 → CompanyProfile。

缓存策略：以 normalize_company(name) 为 key，30 天 TTL。
同一公司被多个 JD 引用时，只查询一次，后续命中缓存直接复用。
"""
from __future__ import annotations

from urllib.parse import urlparse

from jobfinder import cache
from jobfinder.llm_backend import LLMConfig, complete_structured
from jobfinder.logger import get_logger
from jobfinder.schemas import CompanyProfile, normalize_company
from jobfinder.tools import fetch_page

logger = get_logger(__name__)

# job board 域名列表：不能从这些 URL 推断公司官网
_JOB_BOARDS = {
    "indeed.com", "ie.indeed.com", "jobs.ie", "irishjobs.ie",
    "gradireland.com", "linkedin.com", "glassdoor.com",
}


def _extract_city(location: str) -> str:
    """从 location 字符串中提取城市名，用于搜索消歧义。

    示例：
      "Hybrid work in Dublin, County Dublin" → "Dublin"
      "Remote in Cork, County Cork"          → "Cork"
      "DUBLIN 2, County Dublin"              → "DUBLIN 2"
      "Dublin"                               → "Dublin"
    """
    import re
    # 去掉 "Hybrid work in / Remote in / On-site in" 等前缀
    text = re.sub(r"(?i)^(hybrid\s+work\s+in|remote\s+in|on[-\s]?site\s+in)\s*", "", location.strip())
    # 取第一个逗号之前的部分
    city = text.split(",")[0].strip()
    return city


def _search_company(company_name: str, location: str = "") -> str:
    """用 DuckDuckGo 搜索公司官网 URL，再用 Jina Reader 抓取正文。

    流程：DDG 搜索 → 取 top-3 URL → 逐一用 r.jina.ai 抓取 → 返回第一个非空结果。
    全程免费，无需 API Key。
    """
    from ddgs import DDGS
    from urllib.parse import urlparse

    # 跳过这些域名（job board / 社交媒体 / 搜索引擎本身）
    _SKIP_DOMAINS = {
        "indeed.com", "linkedin.com", "glassdoor.com", "irishjobs.ie", "jobs.ie",
        "gradireland.com", "jobserve.com", "monster.com", "reed.co.uk",
        "facebook.com", "twitter.com", "x.com", "instagram.com",
        "youtube.com", "support.google.com", "google.com", "bing.com",
        "wikipedia.org", "wikidata.org",
    }

    city = _extract_city(location) if location else ""
    location_hint = f" {city}" if city else ""
    query = f"{company_name}{location_hint} company official site"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
    except Exception as e:
        logger.debug("DDG 搜索失败：%s — %s", company_name, e)
        return ""

    snippets: list[str] = []
    fetch_attempts = 0
    for r in results:
        url = r.get("href", "")
        domain = urlparse(url).netloc.removeprefix("www.") if url else ""
        if any(s in domain for s in _SKIP_DOMAINS):
            continue
        # 收集摘要作兜底（无论 fetch 是否成功）
        body = r.get("body", "").strip()
        if body:
            snippets.append(f"[{r.get('title', '')}] {body}")
        # 最多 fetch 2 个 URL，每个超时 8 秒，避免长时间阻塞
        if url and fetch_attempts < 2:
            fetch_attempts += 1
            content = fetch_page(url, timeout=8)
            if content:
                logger.debug("DDG → Jina 抓取成功：%s → %d 字符", url, len(content))
                return content

    # 所有 URL fetch 均失败时，拼接 DDG 摘要供 LLM 参考
    if snippets:
        fallback = "\n\n".join(snippets[:3])
        logger.debug("DDG 摘要兜底：%s → %d 字符", company_name, len(fallback))
        return fallback

    logger.debug("DDG 搜索无有效结果：%s", company_name)
    return ""


def _infer_company_url(job_url: str) -> str:
    """从职位 URL 推断公司官网，job board 链接返回空字符串。"""
    if not job_url:
        return ""
    try:
        parsed = urlparse(job_url)
        domain = parsed.netloc.removeprefix("www.")
        if any(board in domain for board in _JOB_BOARDS):
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return ""


def lookup_company(
    company_name: str,
    job_url: str = "",
    llm: LLMConfig | None = None,
    location: str = "",
) -> CompanyProfile:
    """
    查询公司信息并返回 CompanyProfile。

    流程：
      1. 检查缓存（30天TTL）→ 命中直接返回
      2. 从 job_url 推断公司官网，用 Jina Reader 抓取内容
      3. LLM 分析抓取内容 → CompanyProfile
      4. 写缓存
    """
    company_key = normalize_company(company_name)

    cached = cache.get_company_profile(company_key)
    if cached is not None:
        logger.info("Company cache hit: %s", company_name)
        return cached

    if llm is None:
        return CompanyProfile()

    logger.info("Looking up company info: %s", company_name)

    # 抓取公司官网；官网不可用时退回 Jina 搜索
    company_url = _infer_company_url(job_url)
    content = ""
    if company_url:
        content = fetch_page(company_url)
        logger.debug("Fetched company website: %s → %d chars", company_url, len(content))

    if not content:
        content = _search_company(company_name, location)
        logger.debug("Jina search company: %s → %d chars", company_name, len(content))

    if content:
        content_section = f"搜索/官网内容（部分）：\n{content[:3000]}"
    else:
        content_section = "（无法获取公司信息，请仅根据公司名称和常识推断）"

    prompt = f"""根据以下信息提取公司基本情况。

公司名称：{company_name}
{content_section}

请返回以下字段（无法判断的填默认值）：
- size：公司规模
    startup    = 初创公司，约 <50 人
    sme        = 中小企业，约 50~500 人
    enterprise = 大型企业，500 人以上
    unknown    = 无法判断
- industry：所属行业（英文，如 "Technology / AI"、"Finance"、"Healthcare"）
- hq_location：总部城市（英文，无法判断填 ""）
- overview：一到两句话的公司概述（中文）
    格式：这是一家[规模/类型]的[行业]公司，总部位于[地点]，专注于[核心业务]。[可选：补充创立背景/团队规模/核心产品/服务对象等关键信息。]
    若信息不足，填 ""
- career_page_url：如在内容中找到招聘页 URL 则填写，否则填 ""

只返回 JSON，不要额外解释。"""

    try:
        profile = complete_structured(
            prompt=prompt,
            response_schema=CompanyProfile,
            provider=llm.provider,
            model=llm.model,
            system="你是公司信息分析助手，只返回 JSON。",
            _step="公司信息查询",
        )
        cache.save_company_profile(company_key, profile)
        logger.info("Company info cached: %s → size=%s industry=%s", company_name, profile.size, profile.industry)
        return profile
    except Exception as e:
        logger.warning("Company info lookup failed: %s — %s", company_name, e)
        return CompanyProfile()


def enrich_jobs_with_company(
    jobs: list,          # list[JobResult]
    llm: LLMConfig,
    top_n: int = 10,
    min_score: int = 5,
    cb=None,
) -> None:
    """
    对 top_n 条且评分 >= min_score 的职位补充公司信息（原地修改 + 写缓存）。
    已有 company_profile 的职位跳过。
    """
    candidates = [
        j for j in jobs
        if j.company_profile is None
        and (j.assessment is None or j.assessment.score >= min_score)
    ][:top_n]

    if not candidates:
        return

    seen_companies: dict[str, CompanyProfile] = {}
    if cb:
        cb(f"Looking up company info for {len(candidates)} jobs...")

    for job in candidates:
        name = job.company
        key = normalize_company(name)

        if key in seen_companies:
            profile = seen_companies[key]
        else:
            profile = lookup_company(name, job.url, llm, location=job.location)
            seen_companies[key] = profile

        job.company_profile = profile
        cache.update_job_company_profile(job.dedup_key, profile)
