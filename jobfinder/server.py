"""FastAPI Web 服务器：为 Web UI 提供 REST API 和 SSE 进度流。

启动方式：
    uv run jobfinder serve            # 正常模式（使用 jobfinder_cache.db）
    uv run jobfinder serve --mock     # 测试模式（使用 jobfinder_test_cache.db，API 调用真实发生）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading

from pathlib import Path
from typing import AsyncIterator

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from dotenv import load_dotenv

from jobfinder import __version__, cache
from jobfinder.dedup_check import run_dedup_check
from jobfinder.cv_extractor import extract_cv_profile
from jobfinder.cv_reader import read_cv
from jobfinder.logger import get_logger
from jobfinder.llm_backend import (
    AVAILABLE_MODELS,
    DEFAULT_MODELS,
    _COMPAT_PROVIDERS,
    LLMConfig,
    check_provider_connection,
)

# Snapshot of the original model list taken at server start (for mock-mode reset)
_ORIGINAL_AVAILABLE_MODELS: dict[str, list[str]] = {
    k: list(v) for k, v in AVAILABLE_MODELS.items()
}

load_dotenv()

logger = get_logger(__name__)

# ─── 测试模式开关（--mock：使用 jobfinder_test_cache.db，所有 API 调用真实发生） ──

MOCK_MODE: bool = os.getenv("JOBFINDER_MOCK") == "1"
# mock 模式下需要保护的运行时 env var，不允许被 load_dotenv(override=True) 覆盖
_RUNTIME_ENV_KEYS: tuple[str, ...] = ("JOBFINDER_MOCK", "CACHE_DB_PATH") if MOCK_MODE else ()


def _reload_dotenv() -> None:
    """重新加载 .env，但保护 mock 模式的运行时 env var 不被覆盖。"""
    saved = {k: os.environ[k] for k in _RUNTIME_ENV_KEYS if k in os.environ}
    load_dotenv(override=True)
    os.environ.update(saved)

# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="JobFinder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── SSE 进度队列 ─────────────────────────────────────────────────────────────

_progress_q: asyncio.Queue[str] = asyncio.Queue()
_search_running = False
_main_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_loop() -> None:
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    db_path = os.getenv("CACHE_DB_PATH", "jobfinder_cache.db")
    logger.info("JobFinder server started | mock=%s | db=%s", MOCK_MODE, db_path)


def _emit(event_type: str, **kwargs) -> None:
    """从同步线程安全地把 SSE 事件放入队列。"""
    payload = json.dumps({"type": event_type, **kwargs}, ensure_ascii=False)
    if _main_loop and not _main_loop.is_closed():
        _main_loop.call_soon_threadsafe(_progress_q.put_nowait, payload)


# ─── 环境变量工具 ─────────────────────────────────────────────────────────────

def _save_env_key(key: str, value: str) -> None:
    env_path = Path(os.getcwd()) / ".env"
    line = f"{key}={value}"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if re.search(rf"^{re.escape(key)}=", content, re.MULTILINE):
            content = re.sub(rf"^{re.escape(key)}=.*$", line, content, flags=re.MULTILINE)
            env_path.write_text(content, encoding="utf-8")
            return
    with env_path.open("a", encoding="utf-8") as f:
        f.write(f"\n{line}\n")


# ─── Provider / Config API ─────────────────────────────────────────────────────

_PROVIDER_KEY_MAP: dict[str, str] = {
    "claude":  "ANTHROPIC_API_KEY",
    "gemini":  "GEMINI_API_KEY",
    **{p: cfg["key_env"] for p, cfg in _COMPAT_PROVIDERS.items()
       if p not in ("local", "ollama")},
    # 本地部署排在末尾；llama.cpp 无需 API Key，地址通过 LLAMACPP_BASE_URL 配置
    "ollama":  "",
    "local":   "LOCAL_LLM_API_KEY",
}


@app.get("/api/config")
def get_config() -> dict:
    _reload_dotenv()
    providers_status: dict[str, dict] = {}
    for provider, key_env in _PROVIDER_KEY_MAP.items():
        providers_status[provider] = {
            "configured": (not key_env) or bool(os.getenv(key_env)),
            "key_env": key_env,
        }
    default_provider = os.getenv("DEFAULT_PROVIDER", "")
    default_model = os.getenv("DEFAULT_MODEL", "")
    return {
        "providers": providers_status,
        "default_provider": default_provider,
        "default_model": default_model,
        "available_models": AVAILABLE_MODELS,
        "default_models": DEFAULT_MODELS,
        "mock_mode": MOCK_MODE,
        "version": __version__,
        "providers_extra": {
            "adzuna_id":  bool(os.getenv("ADZUNA_APP_ID")),
            "adzuna_key": bool(os.getenv("ADZUNA_APP_KEY")),
        },
    }


class ConfigSaveRequest(BaseModel):
    key: str
    value: str


# 允许通过 /api/config 写入的 env key 白名单
_ALLOWED_ENV_KEYS: set[str] = {
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "XAI_API_KEY", "MISTRAL_API_KEY", "DASHSCOPE_API_KEY",
    "ZHIPUAI_API_KEY", "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY",
    "MINIMAX_API_KEY", "LOCAL_LLM_API_KEY",
    "ADZUNA_APP_ID", "ADZUNA_APP_KEY",
    "LLAMACPP_BASE_URL", "LLAMACPP_API_KEY", "LOCAL_LLM_BASE_URL",
    "DEFAULT_PROVIDER", "DEFAULT_MODEL",
    "JOB_TTL_DAYS", "SESSION_TTL_HOURS", "CACHE_DB_PATH",
}


@app.post("/api/config")
def save_config(req: ConfigSaveRequest) -> dict:
    if req.key not in _ALLOWED_ENV_KEYS:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"不允许设置的配置项：{req.key}")
    _save_env_key(req.key, req.value)
    os.environ[req.key] = req.value
    _reload_dotenv()
    return {"ok": True}


class TestProviderRequest(BaseModel):
    provider: str
    model: str = ""


@app.post("/api/config/test")
def test_provider(req: TestProviderRequest) -> dict:
    _reload_dotenv()
    ok, msg = check_provider_connection(req.provider, req.model or None)
    # Extract model name from message for structured response (client formats display)
    model_used = (req.model or DEFAULT_MODELS.get(req.provider, "")).strip()
    return {"ok": ok, "message": msg, "model": model_used}


# ─── 职位 API ─────────────────────────────────────────────────────────────────

def _job_to_dict(j) -> dict:
    d = j.model_dump(mode="json")
    if j.assessment:
        d["score"] = j.assessment.score
        d["strengths"] = j.assessment.strengths
        d["weaknesses"] = j.assessment.weaknesses
        d["matched_keywords"] = j.assessment.matched_keywords
    else:
        d["score"] = None
    return d


@app.get("/api/jobs")
def get_jobs(limit: int = 200) -> list[dict]:
    jobs = cache.get_recent_jobs(limit)
    jobs = [j for j in jobs if not (j.assessment and not j.assessment.is_relevant)]
    jobs.sort(key=lambda j: (j.assessment.score if j.assessment else -1), reverse=True)
    return [_job_to_dict(j) for j in jobs]


class DeleteRequest(BaseModel):
    dedup_keys: list[str]


@app.delete("/api/jobs")
def delete_jobs(req: DeleteRequest) -> dict:
    n = cache.delete_jobs(req.dedup_keys)
    return {"deleted": n}


@app.post("/api/cache/clear")
def clear_cache() -> dict:
    cache.clear_all()
    logger.info("Cache cleared via Web UI (mock=%s)", MOCK_MODE)
    return {"ok": True}


@app.post("/api/config/clear-keys")
def clear_api_keys() -> dict:
    """Clear all configured API keys from .env and os.environ."""
    key_names = {v for v in _PROVIDER_KEY_MAP.values() if v} | {"ADZUNA_APP_ID", "ADZUNA_APP_KEY"}
    cleared: list[str] = []
    for key in key_names:
        if os.getenv(key):
            _save_env_key(key, "")
            os.environ.pop(key, None)
            cleared.append(key)
    _reload_dotenv()
    logger.info("All API keys cleared via Web UI: %s", cleared)
    return {"ok": True, "cleared": cleared}


@app.post("/api/models/reset")
def reset_models() -> dict:
    """Restore AVAILABLE_MODELS to the snapshot taken at server start (mock mode only)."""
    if not MOCK_MODE:
        raise HTTPException(status_code=403, detail="Only available in mock mode.")
    for provider, models in _ORIGINAL_AVAILABLE_MODELS.items():
        AVAILABLE_MODELS[provider] = list(models)
    logger.info("Model list reset to original snapshot (mock mode)")
    return {"ok": True, "available_models": AVAILABLE_MODELS}


@app.post("/api/models/refresh")
def refresh_models() -> dict:
    """根据已配置的 API Key 拉取各 provider 最新模型列表，更新内存中的 AVAILABLE_MODELS。"""
    from jobfinder.model_fetcher import fetch_all
    _reload_dotenv()
    fetched = fetch_all(top_n=6)
    updated: dict[str, list[str]] = {}
    for provider, ids in fetched.items():
        if ids:                          # 只覆盖拿到数据的 provider
            AVAILABLE_MODELS[provider] = ids
            updated[provider] = ids
    logger.info("Model list refreshed: %s", {p: len(v) for p, v in updated.items()})
    return {"updated": updated}


# ─── CV API ───────────────────────────────────────────────────────────────────

@app.post("/api/cv/parse")
async def parse_cv(
    file: UploadFile = File(...),
    provider: str = Form(default="gemini"),
    model: str = Form(default=""),
) -> dict:
    _ALLOWED_SUFFIXES = {".docx", ".md", ".txt"}
    _MAX_CV_BYTES = 5 * 1024 * 1024  # 5 MB

    suffix = Path(file.filename or "cv.docx").suffix.lower() or ".docx"
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{suffix}'. Allowed: .docx / .md / .txt")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        if len(content) > _MAX_CV_BYTES:
            raise HTTPException(status_code=413, detail="CV file too large (max 5 MB)")
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        cv_text = read_cv(tmp_path)
        _model = model or DEFAULT_MODELS.get(provider, "")
        llm = LLMConfig(provider=provider, model=_model)
        logger.info("CV parse started | file=%s provider=%s model=%s", file.filename, provider, _model)
        profile = extract_cv_profile(cv_text, llm=llm)
        cv_hash = hashlib.sha256(cv_text.encode()).hexdigest()
        logger.info("CV parse done | hash=%s seniority=%s skills=%d", cv_hash[:8], profile.seniority, len(profile.skills))
        return {"cv_hash": cv_hash, "profile": profile.model_dump(mode="json")}
    except Exception as e:
        logger.error("CV parse failed | file=%s error=%s", file.filename, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── Title 发现 API ───────────────────────────────────────────────────────────

class DiscoverTitlesRequest(BaseModel):
    cv_hash: str
    provider: str = "gemini"
    model: str = ""
    countries: list[str] = ["us", "gb"]
    profile: dict | None = None  # 前端传来的 CVProfile，缓存丢失时作兜底


@app.post("/api/titles/discover")
def discover_titles_endpoint(req: DiscoverTitlesRequest) -> dict:
    from jobfinder.schemas import CVProfile
    from jobfinder.title_discovery import discover_titles

    profile = cache.get_cv_profile(req.cv_hash) or cache.get_latest_cv_profile()
    if profile is None and req.profile:
        try:
            profile = CVProfile.model_validate(req.profile)
            cache.save_cv_profile(req.cv_hash, profile)  # 重新写入缓存
            logger.info("CV profile restored from request body | hash=%s", req.cv_hash[:8])
        except Exception as e:
            logger.warning("Failed to restore CV profile from request body: %s", e)
    if profile is None:
        raise HTTPException(status_code=400, detail="找不到 CV 数据，请先上传 CV。")

    cache_key = f"{req.cv_hash}::{'_'.join(sorted(req.countries))}"
    cached = cache.get_title_cache(cache_key)
    if cached:
        return cached

    _model = req.model or DEFAULT_MODELS.get(req.provider, "")
    llm = LLMConfig(provider=req.provider, model=_model)
    logger.info("Title discovery started | cv_hash=%s countries=%s provider=%s", req.cv_hash[:8], req.countries, req.provider)

    try:
        result = discover_titles(
            skills=profile.skills,
            cv_summary=profile.summary,
            seniority=profile.seniority,
            llm=llm,
            top_keywords=8,
            countries=req.countries,
        )
        data = {
            "titles": [t.model_dump() for t in result.titles],
            "keywords_used": result.keywords_used,
        }
        cache.save_title_cache(cache_key, json.dumps(data))
        logger.info("Title discovery done | titles=%d", len(result.titles))
        return data
    except Exception as e:
        logger.error("Title discovery failed | error=%s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── 搜索 API ─────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    cv_hash: str
    roles: list[str]
    location: str
    provider: str = "gemini"
    model: str = ""
    refresh: bool = False
    language: str = "zh"
    limit_per_role: int = 100
    linkedin_limit_per_role: int = 30
    hours_old: int | None = 72


@app.post("/api/search")
async def start_search(req: SearchRequest, background_tasks: BackgroundTasks) -> dict:
    global _search_running
    if _search_running:
        raise HTTPException(status_code=409, detail="搜索正在进行中，请等待完成。")
    _search_running = True
    background_tasks.add_task(_run_search_task, req)
    return {"status": "started"}


async def _run_search_task(req: SearchRequest) -> None:
    """在后台线程运行真实搜索，通过 on_job 逐条 emit 职位到前端。"""
    def on_job(key: str) -> None:
        job = cache.get_job(key)
        if job:
            _emit("job", job=_job_to_dict(job))

    def run() -> None:
        global _search_running
        import time as _time
        from jobfinder.telemetry import telemetry
        try:
            from jobfinder.agent import run_search

            profile = cache.get_cv_profile(req.cv_hash)
            if profile is None:
                logger.warning("cv_hash %s not found, falling back to latest profile", req.cv_hash[:8])
                profile = cache.get_latest_cv_profile()
            if profile is None:
                logger.error("Search failed: no CV profile in DB (cv_hash=%s)", req.cv_hash[:8])
                _emit("error", msg="CV profile not found. Please upload your CV first.")
                return

            profile = profile.model_copy(update={"preferred_roles": req.roles})

            _model = req.model or DEFAULT_MODELS.get(req.provider, "")
            llm = LLMConfig(provider=req.provider, model=_model)
            logger.info(
                "Search started | location=%s roles=%s provider=%s model=%s refresh=%s",
                req.location, req.roles, req.provider, _model, req.refresh,
            )

            telemetry.reset()
            _search_start = _time.monotonic()

            dedup_keys, pipeline_stats = run_search(
                profile=profile,
                location=req.location,
                llm=llm,
                on_progress=lambda msg: _emit("progress", msg=msg),
                on_job=on_job,
                force_refresh=req.refresh,
                language=req.language,
                limit_per_role=req.limit_per_role,
                linkedin_limit_per_role=req.linkedin_limit_per_role,
                hours_old=req.hours_old,
            )

            elapsed = _time.monotonic() - _search_start
            with telemetry._lock:
                tokens_in  = sum(r.input_tokens  for r in telemetry.llm_records)
                tokens_out = sum(r.output_tokens for r in telemetry.llm_records)

            logger.info(
                "Search done | jobs=%d elapsed=%.1fs tokens_in=%d tokens_out=%d",
                len(dedup_keys), elapsed, tokens_in, tokens_out,
            )
            cache.save_search_stats(
                location=req.location,
                roles=req.roles,
                provider=req.provider,
                model=_model,
                elapsed=elapsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                jobs_found=len(dedup_keys),
                funnel=pipeline_stats.to_dict(),
                cv_hash=req.cv_hash,
            )
            try:
                dedup_report = run_dedup_check(list(dedup_keys))
            except Exception as _de:
                logger.warning("Dedup check failed: %s", _de)
                dedup_report = {"total": 0, "l1": 0, "l2": 0, "l2_items": []}
            _emit("done", count=len(dedup_keys),
                  elapsed=round(elapsed, 1),
                  tokens_in=tokens_in, tokens_out=tokens_out,
                  pipeline_stats=pipeline_stats.to_dict(),
                  dedup=dedup_report)
        except Exception as e:
            logger.error("Search failed | error=%s", e, exc_info=True)
            _emit("error", msg=str(e))
        finally:
            _search_running = False

    threading.Thread(target=run, daemon=True).start()


@app.get("/api/search/progress")
async def search_progress() -> StreamingResponse:
    async def event_gen() -> AsyncIterator[str]:
        loop = asyncio.get_event_loop()
        _timeout_minutes = int(os.getenv("SSE_TIMEOUT_MINUTES", "30"))
        _chunk = _timeout_minutes * 60          # 每轮等待时长（秒）
        _max_extensions = 4                     # 最多续期次数（防无限挂起）
        extensions = 0
        deadline = loop.time() + _chunk
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                if _search_running and extensions < _max_extensions:
                    # 搜索仍在进行，续期一轮
                    extensions += 1
                    deadline = loop.time() + _chunk
                    logger.info(
                        "SSE progress: search still running, extending deadline (%d/%d)",
                        extensions, _max_extensions,
                    )
                    yield ": keepalive\n\n"
                    continue
                logger.warning(
                    "SSE progress: deadline reached after %d min, closing",
                    _timeout_minutes * (extensions + 1),
                )
                yield "data: {\"type\":\"timeout\"}\n\n"
                break
            try:
                # 每 15 秒发一次 keepalive 注释，防止代理/浏览器断开连接
                msg = await asyncio.wait_for(_progress_q.get(), timeout=min(15.0, remaining))
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"data: {msg}\n\n"
            data = json.loads(msg)
            if data.get("type") in ("done", "error", "timeout"):
                break

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/search/status")
def search_status() -> dict:
    return {"running": _search_running, "mock_mode": MOCK_MODE}


# ─── 统计 API ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(limit: int = 50) -> dict:
    """返回搜索历史记录和全量累计统计。"""
    return {
        "records": cache.get_search_stats(limit=limit),
        "summary": cache.get_stats_summary(),
    }


@app.delete("/api/stats")
def delete_stats() -> dict:
    """清空全部搜索历史记录。"""
    cache.clear_search_stats()
    return {"ok": True}


# ─── 日志 API ────────────────────────────────────────────────────────────────

@app.get("/api/logs")
def get_logs(lines: int = 200, level: str = "") -> dict:
    """返回日志文件最后 N 行，可按 level 过滤（ERROR/WARNING/INFO/DEBUG）。"""
    from jobfinder.logger import _LOG_FILE
    log_path = Path(_LOG_FILE) if _LOG_FILE else None
    if not log_path or not log_path.exists():
        return {"lines": [], "path": str(log_path or "disabled")}

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        if level:
            lvl = level.upper()
            all_lines = [l for l in all_lines if f"[{lvl}]" in l]
        tail = all_lines[-lines:]
        return {"lines": tail, "path": str(log_path)}
    except Exception as e:
        logger.error("Failed to read log file: %s", e)
        return {"lines": [f"[ERROR] 读取日志失败：{e}"], "path": str(log_path)}


# ─── 静态 HTML ────────────────────────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html_path = _TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>UI not found</h1>", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
