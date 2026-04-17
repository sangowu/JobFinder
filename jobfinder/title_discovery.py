"""
基于 CV 技能词，从 Adzuna US 抓取真实 title 样本，
再由 LLM 归纳聚类，输出按频次排序的候选 title 列表。
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from pydantic import BaseModel

from jobfinder.llm_backend import LLMConfig, Provider, complete_structured
from jobfinder.logger import get_logger

logger = get_logger(__name__)

_ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"
_RESULTS_PER_KEYWORD = 50   # Adzuna 单次上限

# 全局速率锁：保证同一 API key 的请求间隔 ≥ 1.2s，无论有多少线程
_rate_lock = threading.Lock()
_last_request_at: float = 0.0
_MIN_INTERVAL = 1.2  # 秒


# ── 搜索关键词选择 ────────────────────────────────────────────────────────────

class _MixedKeywords(BaseModel):
    role_phrases: list[str]   # 角色级短语，用于捕获主流高频 title
    tech_terms: list[str]     # 技术词，用于捕获新兴小众 title


def _generate_search_keywords(
    skills: list[str],
    cv_summary: str,
    provider: Provider,
    model: str,
    n: int = 6,
) -> list[str]:
    """
    混合策略：
    - role_phrases（角色短语）→ 命中主流高频 title（AI Engineer、ML Engineer）
    - tech_terms（技术词）    → 命中新兴小众 title（Agent Engineer、LLM Platform Engineer）
    两类各取一半，合并后查询。
    """
    skills_text = ", ".join(skills[:30])
    half = max(n // 2, 2)
    prompt = f"""候选人背景：{cv_summary}
候选人技能列表：{skills_text}

为在 Adzuna 招聘网站发现该候选人适合的职位 title，生成两类搜索关键词：

1. role_phrases（{half} 个）：角色级短语，直接对应职位名称
   目标：广泛覆盖候选人专业领域的主流及相邻 title
   要求：
   - 根据候选人的实际技能和背景生成，不限定任何特定行业
   - 覆盖"精准匹配"到"宽泛兜底"的梯度
   - 最后一个必须是兜底宽泛词，确保在专精词命中率低时仍有足量 title 样本
   - 不同短语之间不能重复，尽量覆盖更广的角色类型

2. tech_terms（{half} 个）：候选人核心专业技能词或工具名
   目标：搜到使用该技能/工具的岗位，发现该领域内的细分 title
   要求：每个词能在 Adzuna 独立搜索并返回有意义的结果，
        避免过于通用（如 "Excel"、"Microsoft Office"）或过于冷僻的词

只返回 JSON。"""
    try:
        result = complete_structured(
            prompt=prompt,
            response_schema=_MixedKeywords,
            provider=provider,
            model=model,
            system="你是招聘关键词专家，只返回 JSON。",
            _step="搜索关键词生成",
        )
        keywords = result.role_phrases[:half] + result.tech_terms[:half]
        logger.info("Mixed keywords role=%s tech=%s", result.role_phrases[:half], result.tech_terms[:half])
        return keywords
    except Exception as e:
        logger.warning("Keyword generation failed, falling back to raw skills: %s", e)
        return skills[:n]


# ── Adzuna 查询 ───────────────────────────────────────────────────────────────

def _rate_limited_fetch(keyword: str, country: str, retries: int = 2) -> list[str]:
    """
    带全局速率限制的 Adzuna 查询。
    _rate_lock 保证所有线程合计请求频率 ≤ 1/_MIN_INTERVAL req/s，避免 429。
    """
    global _last_request_at
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        logger.warning("ADZUNA_APP_ID / ADZUNA_APP_KEY 未配置")
        return []

    for attempt in range(retries + 1):
        # 全局速率控制：持锁期间等待并记录发出时间
        with _rate_lock:
            wait = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()

        try:
            resp = requests.get(
                f"{_ADZUNA_BASE}/{country}/search/1",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": keyword,
                    "results_per_page": _RESULTS_PER_KEYWORD,
                    "content-type": "application/json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            titles = [r["title"] for r in data.get("results", []) if r.get("title")]
            logger.debug("Adzuna [%s@%s] → %d titles", keyword, country, len(titles))
            return titles
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (429, 503) and attempt < retries:
                backoff = 2 ** (attempt + 1)
                logger.debug("Adzuna 限流 [%s@%s]，%ds 后重试", keyword, country, backoff)
                time.sleep(backoff)
            else:
                logger.warning("Adzuna 查询失败 [%s@%s]：%s", keyword, country, e)
                return []
        except Exception as e:
            logger.warning("Adzuna 查询失败 [%s@%s]：%s", keyword, country, e)
            return []
    return []


def _collect_raw_titles(keywords: list[str], countries: list[str] = ("us", "gb")) -> Counter:
    """
    2 个线程并行发出请求，全局速率锁保证总频率不超限。
    网络 I/O 可以重叠，整体耗时约比纯顺序减少 30-40%。
    """
    queries = [(kw, c) for kw in keywords for c in countries]
    counter: Counter = Counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_rate_limited_fetch, kw, c): (kw, c) for kw, c in queries}
        for fut in as_completed(futures):
            for title in fut.result():
                counter[title] += 1
    return counter


# ── 结构性噪音过滤（Python 层，不依赖 LLM，与领域无关）───────────────────────

# 逗号/&/and 连接多个技术词的堆砌模式
_STACKED_PATTERN = re.compile(
    r".+,\s*.+(?:\s+(?:&|and)\s+).+",   # "X, Y & Z ..." 或 "X, Y and Z ..."
    re.IGNORECASE,
)
_MAX_TITLE_WORDS = 8   # 超过此词数视为噪音（正常职位名很少超过 6 词）
_MIN_TITLE_FREQ  = 1   # 只出现 1 次的 title 通常是拼写错误或极度个性化


def _clean_raw_counter(counter: Counter) -> Counter:
    """
    在送入 LLM 之前，用纯结构规则过滤明显噪音：
    1. 堆砌格式：title 中含 "X, Y & Z" 或 "X, Y and Z"
    2. 超长 title：词数 > _MAX_TITLE_WORDS
    3. 极低频：count <= _MIN_TITLE_FREQ（孤立噪音）

    规则基于 title 的结构特征，与招聘领域无关。
    """
    cleaned: Counter = Counter()
    removed = 0
    for title, cnt in counter.items():
        if _STACKED_PATTERN.match(title):
            logger.debug("Filter stacked noise title: %s", title)
            removed += 1
            continue
        if len(title.split()) > _MAX_TITLE_WORDS:
            logger.debug("Filter overly long title: %s", title)
            removed += 1
            continue
        if cnt <= _MIN_TITLE_FREQ:
            removed += 1
            continue
        cleaned[title] = cnt
    if removed:
        logger.info("Structural filter: removed %d noise titles, kept %d", removed, len(cleaned))
    return cleaned


# ── LLM 聚类 ─────────────────────────────────────────────────────────────────

class _TitleGroup(BaseModel):
    canonical: str        # 规范化后的 title（英文，市场通用写法）
    count: int            # 归并后的总频次
    variants: list[str]   # 被归入此组的原始 title 列表


class _ClusterResult(BaseModel):
    titles: list[_TitleGroup]


class DiscoverResult:
    """discover_titles 的返回值，包含 title 列表和实际使用的搜索关键词。"""
    __slots__ = ("titles", "keywords_used")

    def __init__(self, titles: list[_TitleGroup], keywords_used: list[str]) -> None:
        self.titles = titles
        self.keywords_used = keywords_used


def _cluster_titles(
    raw_counter: Counter,
    cv_summary: str,
    cv_skills: list[str],
    seniority: str,
    provider: Provider,
    model: str,
) -> list[_TitleGroup]:
    top_items = raw_counter.most_common(80)
    titles_text = "\n".join(f"{title} ({cnt})" for title, cnt in top_items)
    skills_text = ", ".join(cv_skills[:15])

    seniority_rule = {
        "intern":   "只保留含 intern / internship / placement 的 title，删除其他所有级别",
        "new_grad": "删除含 Senior/Sr/Staff/Lead/Principal/Manager/Director/Head/Architect/VP 的 title",
        "junior":   "删除含 Senior/Staff/Principal/Director/Head/VP 的 title",
        "mid":      "删除含 Director/Head/VP/Principal/Staff 的 title",
    }.get(seniority, "")

    prompt = f"""你是职位 title 归类专家。以下是从美国招聘市场收集到的职位 title 及出现次数。

候选人背景：{cv_summary}
候选人技能：{skills_text}
候选人资历级别：{seniority}

原始 title 列表（title + 出现次数）：
{titles_text}

归并规则（严格执行）：
1. 核心角色相同的 title 必须合并，count 相加。归并时剥离以下修饰语：
   - 括号内的技术词：(AWS)、(Python)、(Remote) 等
   - "with X" 后缀：with Security Clearance、with X Experience 等
   - 逗号后的专向描述：Software Engineer, Full Stack → Software Engineer
   - 技术栈前缀：AWS Backend Developer → Backend Developer
   例：
   "Backend Developer" + "Backend Developer with Security Clearance" + "AWS Backend Developer" + "Node.js Backend Developer"
   → canonical="Backend Developer", count=全部相加

2. 跨技术栈但核心角色相同的也要合并：
   "Frontend Engineer" + "Front End Engineer" + "Front-End Engineer"
   → canonical="Frontend Engineer"

3. slash（/）组合 title 按以下规则处理：
   a. 两侧是不同角色时，拆分后分别归入对应组：
      "Product Designer / UX Designer" → 分别计入 "Product Designer" 和 "UX Designer"
   b. 两侧是同义缩写时，归入其中一个 canonical 不拆分：
      "UI/UX Designer" → 归入 "UX Designer"

4. 资历前缀合并：Junior / Associate / Trainee / Graduate / Entry-level 前缀的 title
   归入对应的无前缀 canonical，count 相加：
   "Junior Data Analyst" + "Associate Data Analyst" + "Graduate Data Analyst" + "Data Analyst"
   → canonical="Data Analyst", count=全部相加

5. 删除与候选人专业背景明显不相关的 title（根据候选人技能和背景判断，不要预设特定行业）

6. 级别过滤：{seniority_rule if seniority_rule else "保留适合候选人资历的 title"}

7. 删除过于宽泛的单词 title（"Engineer"、"Developer"、"Intern" 单独出现时）

8. 按 count 降序排列，canonical 使用市场最常见写法

只返回 JSON，不要额外解释。"""

    try:
        result = complete_structured(
            prompt=prompt,
            response_schema=_ClusterResult,
            provider=provider,
            model=model,
            system="你是职位 title 归类专家，只返回 JSON。",
            _step="Title 聚类归并",
        )
        return result.titles
    except Exception as e:
        logger.warning("LLM 聚类失败：%s，回退原始频次排序", e)
        return [
            _TitleGroup(canonical=title, count=cnt, variants=[title])
            for title, cnt in raw_counter.most_common(20)
        ]


# ── CV 匹配过滤 ───────────────────────────────────────────────────────────────

class _FilterResult(BaseModel):
    titles: list[str]   # 过滤 + 排序后的 canonical title 列表（最匹配排最前）


def _filter_by_cv(
    groups: list[_TitleGroup],
    cv_summary: str,
    cv_skills: list[str],
    seniority: str,
    provider: Provider,
    model: str,
    max_titles: int = 8,
) -> list[str]:
    """
    将聚类后的 title 列表交给 LLM，结合 CV 判断候选人对每个 title 的竞争力，
    过滤掉明显不匹配的，剩余按匹配度降序排列。
    """
    titles_text = "\n".join(
        f"- {g.canonical} (市场频次 {g.count})" for g in groups
    )
    skills_text = ", ".join(cv_skills[:20])

    prompt = f"""你是资深招聘顾问。请根据候选人的 CV 信息，从以下 title 列表中筛选出该候选人真正有竞争力申请的职位。

候选人背景：{cv_summary}
候选人技能：{skills_text}
候选人资历级别：{seniority}

候选 title 列表（附市场频次供参考）：
{titles_text}

筛选标准：
1. 候选人的技能与该 title 的核心要求有实质重叠
2. 该 title 在市场上真实存在（不是边缘小众职位）
3. 符合候选人的资历级别
4. 按候选人的匹配度从高到低排列
5. 最多返回 {max_titles} 个 title，优先选覆盖面广、市场频次高的

只返回 titles 字段（字符串列表），不要额外解释。"""

    try:
        result = complete_structured(
            prompt=prompt,
            response_schema=_FilterResult,
            provider=provider,
            model=model,
            system="你是招聘顾问，只返回 JSON。",
            _step="CV 匹配过滤",
        )
        logger.info("CV 筛选后保留 %d 个 title", len(result.titles))
        return result.titles
    except Exception as e:
        logger.warning("CV 筛选失败，返回全部聚类结果：%s", e)
        return [g.canonical for g in groups]


# ── 公共入口 ──────────────────────────────────────────────────────────────────

def discover_titles(
    skills: list[str],
    cv_summary: str,
    seniority: str = "unknown",
    llm: LLMConfig | None = None,
    top_keywords: int = 5,
    countries: list[str] | None = None,
    max_titles: int = 5,
    # 兼容旧参数
    provider: Provider = "gemini",
    model: str = "gemini-2.0-flash",
) -> DiscoverResult:
    """
    主入口：从 CV 技能中选出最具代表性的关键词 → Adzuna US+UK 抓取真实 title →
    LLM 归纳聚类 → CV 匹配过滤 → 按频次排序返回候选 title 列表。
    """
    from jobfinder.llm_backend import DEFAULT_MODELS
    effective_llm = llm or LLMConfig(provider=provider, model=model)
    _provider, _model = effective_llm.provider, effective_llm.model

    if countries is None:
        countries = ["us", "gb"]

    if not skills:
        logger.warning("No skills found, skipping title discovery")
        return DiscoverResult(titles=[], keywords_used=[])

    # Step 1：LLM 基于候选人背景自由生成最佳搜索短语（不限于 CV 原词）
    keywords = _generate_search_keywords(skills, cv_summary, _provider, _model, n=top_keywords)

    # Step 2：Adzuna 查询（US + UK），收集原始 title
    logger.info("Adzuna title discovery: keywords=%s markets=%s", keywords, countries)
    raw_counter = _collect_raw_titles(keywords, countries=countries)

    if not raw_counter:
        logger.warning("Adzuna returned no titles")
        return DiscoverResult(titles=[], keywords_used=keywords)

    # Step 3：结构过滤（Python 层）→ LLM 聚类
    raw_counter = _clean_raw_counter(raw_counter)
    if not raw_counter:
        logger.warning("No titles remaining after structural filter")
        return DiscoverResult(titles=[], keywords_used=keywords)
    logger.info("%d titles remaining after filter, starting clustering", len(raw_counter))
    groups = _cluster_titles(raw_counter, cv_summary, skills, seniority, _provider, _model)

    # Step 4：按 CV 匹配度二次过滤 + 排序（提前告知模型上限，减少无效输出）
    filtered = _filter_by_cv(groups, cv_summary, skills, seniority, _provider, _model,
                             max_titles=max_titles + 2)  # 留 2 个余量给最终截断

    count_map = {g.canonical: g.count for g in groups}
    titles = [
        _TitleGroup(canonical=t, count=count_map.get(t, 0), variants=[])
        for t in filtered[:max_titles]
    ]
    logger.info("Final output %d titles (max %d)", len(titles), max_titles)
    return DiscoverResult(titles=titles, keywords_used=keywords)
