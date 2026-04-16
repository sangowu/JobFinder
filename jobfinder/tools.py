"""Agent 工具定义：供 Claude tool use 调用的函数集合。"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from urllib.parse import urlparse

import requests

from jobfinder import cache
from jobfinder.logger import get_logger
from jobfinder.schemas import JobAssessment, JobResult, make_dedup_key

log = get_logger(__name__)

# ─── Tavily 搜索 ──────────────────────────────────────────────────────────────

_TAVILY_MAX_QUERY = 400


def _search_tavily(role: str, query: str, location: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient  # 懒加载，不强制安装
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return []
    client = TavilyClient(api_key=api_key)
    base = f"{role} jobs in {location} "
    remaining = _TAVILY_MAX_QUERY - len(base)
    trimmed_query = query[:max(0, remaining)].strip()
    full_query = (base + trimmed_query).strip()
    try:
        log.debug("Tavily 搜索：%s", full_query)
        response = client.search(
            query=full_query,
            search_depth="basic",
            max_results=max_results,
            include_answer=False,
        )
        results = response.get("results", [])
        log.info("Tavily: %s @ %s → %d 条", role, location, len(results))
        return results
    except Exception as e:
        log.warning("Tavily 搜索失败：%s", e)
        return []


# ─── 搜索入口（仅 Tavily）────────────────────────────────────────────────────


def search_jobs(
    query: str,
    location: str,
    role: str,
    max_results: int = 10,
    graduate: bool = False,
) -> list[dict]:
    """调用 Tavily 搜索职位，返回结果列表。"""
    return _search_tavily(role, query, location, max_results)


# ─── Jina Reader 抓取全文 ─────────────────────────────────────────────────────


_VERIFICATION_SIGNALS = (
    "just a moment",
    "enable javascript and cookies",
    "checking your browser",
    "please wait",
    "cloudflare",
    "ddos protection",
    "ray id",
)


def is_verification_page(content: str) -> bool:
    """判断抓取内容是否为 Cloudflare / 人机验证页而非真实 JD。"""
    if not content or len(content) < 200:
        return True
    sample = content[:1000].lower()
    return sum(sig in sample for sig in _VERIFICATION_SIGNALS) >= 2


_PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def _fetch_with_playwright(url: str) -> str:
    """用 Playwright 无头浏览器抓取需要 JS 的页面（如 Indeed 详情页）。"""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=_PLAYWRIGHT_UA)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            text = await page.inner_text("body")
            await browser.close()
            return text[:15000]
    except Exception as e:
        log.warning("Playwright fetch 失败：%s - %s", url, e)
        return ""


def fetch_page_playwright(url: str) -> str:
    """同步封装：用 Playwright 抓取受 Cloudflare 保护的页面。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(asyncio.run, _fetch_with_playwright(url)).result()
        return asyncio.run(_fetch_with_playwright(url))
    except Exception as e:
        log.warning("fetch_page_playwright 异常：%s - %s", url, e)
        return ""


def fetch_page(url: str, timeout: int = 15) -> str:
    """
    通过 Jina Reader 抓取网页正文，返回纯文本。
    失败或返回验证页时返回空字符串。
    """
    try:
        log.debug("fetch_page: %s", url)
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            text = resp.text[:15000]
            if is_verification_page(text):
                log.warning("fetch_page 返回验证页，跳过：%s", url)
                return ""
            log.debug("fetch_page 成功，长度 %d 字符", len(resp.text))
            return text
        log.warning("fetch_page 失败：%s %s", resp.status_code, url)
        return ""
    except Exception as e:
        log.warning("fetch_page 异常：%s - %s", url, e)
        return ""


# ─── 主动验证职位是否仍有效 ──────────────────────────────────────────────────

_INACTIVE_PATTERN = re.compile(
    r"\b("
    r"(this\s+)?(job|position|vacancy|role|listing|posting)\s+(is\s+)?(no longer|not)\s+(available|accepting|open|active)"
    r"|applications?\s+(are\s+)?(now\s+)?(closed|no longer being accepted)"
    r"|no longer (accepting|taking)\s+applications?"
    r"|(position|vacancy|role)\s+(has been\s+)?(filled|closed|removed)"
    r"|this\s+(role|position|job)\s+has\s+(been\s+)?(filled|closed)"
    r"|sorry[^.]*no longer available"
    r"|recruitment\s+(for\s+this\s+(role|position)\s+)?(has\s+)?(closed|ended|finished)"
    r")\b",
    re.IGNORECASE,
)


def verify_job_active(url: str) -> dict:
    """
    通过 Jina Reader 抓取职位页面，判断是否仍在招募。
    返回 {"active": bool, "reason": str}
    """
    text = fetch_page(url)
    if not text:
        return {"active": False, "reason": "fetch_failed"}
    if _INACTIVE_PATTERN.search(text):
        return {"active": False, "reason": "posting_closed"}
    return {"active": True, "reason": "ok"}


# ─── 失败 URL 管理 ────────────────────────────────────────────────────────────


def check_failed_urls(urls: list[str]) -> list[str]:
    """返回 urls 中已在失败黑名单内的 URL 列表。"""
    failed = cache.get_failed_urls(urls)
    return list(failed)


def record_failed_url(url: str, reason: str) -> None:
    """记录失败 URL 到黑名单。"""
    cache.record_failed_url(url, reason)


# ─── 缓存读写 ─────────────────────────────────────────────────────────────────


def read_cache(session_key: str) -> dict | None:
    """
    读取 SearchSession 缓存。
    返回 {session, jobs} 或 None（未命中/已过期）。
    """
    session = cache.get_session(session_key)
    if session is None:
        return None
    jobs = cache.get_jobs_by_keys(session.job_dedup_keys)
    return {"session": session.model_dump(mode="json"), "jobs": [j.model_dump(mode="json") for j in jobs]}


def write_cache(job_data: dict, session_key: str | None = None) -> str:
    """
    写入单条 JobResult 到缓存，返回 dedup_key。
    job_data 字段：title, company, location, url, description_snippet,
                   sources, expires_at（可选）, is_complete
    """
    url = job_data.get("url", "")
    sources = job_data.get("sources") or []
    # 自动从 URL 提取域名作为来源（agent 不传时兜底）
    if not sources and url:
        domain = _extract_domain(url)
        if domain:
            sources = [domain]

    raw_assessment = job_data.get("assessment")
    assessment: JobAssessment | None = None
    if isinstance(raw_assessment, JobAssessment):
        assessment = raw_assessment
    elif isinstance(raw_assessment, dict):
        assessment = JobAssessment.model_validate(raw_assessment)
    elif raw_assessment is not None and hasattr(raw_assessment, "model_dump"):
        # 兼容 _JDAssessment 等 Pydantic 子类（含额外字段，model_validate 自动忽略）
        assessment = JobAssessment.model_validate(raw_assessment.model_dump())

    job = JobResult(
        title=job_data.get("title", ""),
        company=job_data.get("company", ""),
        location=job_data.get("location", ""),
        url=url,
        description_snippet=job_data.get("description_snippet", ""),
        sources=sources,
        date_posted=job_data.get("date_posted", ""),
        expires_at=_parse_date(job_data.get("expires_at")),
        is_complete=job_data.get("is_complete", True),
        assessment=assessment,
    )
    cache.save_job(job)
    log.info("write_cache: %s @ %s [%s]", job.title, job.company, job.dedup_key)
    return job.dedup_key


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except Exception:
        return ""


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from dateutil import parser as dateutil_parser
        return dateutil_parser.parse(value)
    except Exception:
        return None


# ─── Claude tool schema 定义 ──────────────────────────────────────────────────
# 供 llm_backend.complete_with_tools 使用

TOOL_SCHEMAS = [
    {
        "name": "search_jobs",
        "description": "调用 Tavily 搜索指定角色和地点的职位，返回原始结果列表",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "附加搜索关键词，只选 3-5 个最核心的技能，不要传入完整技能列表，总长度控制在 50 字符以内"},
                "location": {"type": "string", "description": "目标城市或 Remote"},
                "role": {"type": "string", "description": "目标职位名称"},
                "max_results": {"type": "integer", "default": 15},
            },
            "required": ["query", "location", "role"],
        },
    },
    {
        "name": "fetch_page",
        "description": "通过 Jina Reader 抓取网页完整正文，用于 snippet 信息不足时",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的职位页面 URL"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "check_failed_urls",
        "description": "检查 URL 列表中哪些已在失败黑名单内，返回需跳过的 URL 列表",
        "input_schema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["urls"],
        },
    },
    {
        "name": "record_failed_url",
        "description": "将无法解析的 URL 记录到失败黑名单",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "失败原因，如 login_required / page_offline / js_rendered / parse_failed",
                },
            },
            "required": ["url", "reason"],
        },
    },
    {
        "name": "write_cache",
        "description": "将解析好的职位信息写入缓存，返回 dedup_key",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "company": {"type": "string"},
                "location": {"type": "string"},
                "url": {"type": "string"},
                "description_snippet": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "expires_at": {"type": "string", "description": "ISO8601 或自然语言日期，可为 null"},
                "is_complete": {"type": "boolean"},
            },
            "required": ["title", "company", "url"],
        },
    },
]


# ─── 工具分发函数 ─────────────────────────────────────────────────────────────


def dispatch(tool_name: str, tool_input: dict) -> str:
    """根据工具名调用对应函数，返回 JSON 字符串结果。"""
    import json

    if tool_name == "search_jobs":
        results = search_jobs(**tool_input)
        return json.dumps(results, ensure_ascii=False)

    elif tool_name == "fetch_page":
        content = fetch_page(tool_input["url"])
        return json.dumps({"content": content}, ensure_ascii=False)

    elif tool_name == "check_failed_urls":
        failed = check_failed_urls(tool_input["urls"])
        return json.dumps({"failed_urls": failed}, ensure_ascii=False)

    elif tool_name == "record_failed_url":
        record_failed_url(tool_input["url"], tool_input["reason"])
        return json.dumps({"ok": True})

    elif tool_name == "write_cache":
        dedup_key = write_cache(tool_input)
        return json.dumps({"dedup_key": dedup_key}, ensure_ascii=False)

    else:
        return json.dumps({"error": f"未知工具：{tool_name}"})
