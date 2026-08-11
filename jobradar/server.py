"""FastAPI Web 服务器：为 Web UI 提供 REST API 和 SSE 进度流。

启动方式：
    uv run jobradar serve            # 正常模式（使用 data/jobradar_cache.db）
    uv run jobradar serve --mock     # 测试模式（使用 data/jobradar_test_cache.db，API 调用真实发生）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import AsyncIterator, Callable

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from jobradar import __version__, application_store, cache

# 仅为副作用导入：metrics.py 顶层的 Counter/Histogram/Gauge 在模块被 import 时
# 才注册进 prometheus_client 全局注册表。email_sync 已经间接 import 了它，这里
# 再显式 import 一次，避免日后 email_sync 重构掉该 import 时指标静默消失。
from jobradar import metrics as _metrics  # noqa: F401
from jobradar.assessment import TITLE_RELEVANCE_PROMPT_VERSION
from jobradar.cover_letter import generate_cover_letter
from jobradar.cv_extractor import PROMPT_VERSION as CV_PROMPT_VERSION
from jobradar.cv_extractor import extract_cv_profile
from jobradar.cv_optimization import generate_cv_optimization
from jobradar.cv_reader import read_cv
from jobradar.dedup_check import run_dedup_check
from jobradar.email_sync import (
    clear_email_tracking_data,
    complete_google_oauth,
    disconnect_google_email,
    email_sync_configured,
    gmail_message_url,
    gmail_thread_url,
    google_oauth_authorization_url,
    google_oauth_configured,
    reanalyse_email,
    set_automatic_sync_paused,
    sync_email,
)
from jobradar.filters import TITLE_GATE_VERSION
from jobradar.interview_prep import generate_interview_prep
from jobradar.jd_profile import PROMPT_VERSION as JD_PROFILE_PROMPT_VERSION
from jobradar.jd_profile import extract_jd_profile
from jobradar.llm_backend import (
    AVAILABLE_MODELS,
    DEFAULT_MODELS,
    LLMConfig,
    check_provider_connection,
)
from jobradar.logger import get_logger
from jobradar.matching import PROMPT_VERSION as MATCH_PROMPT_VERSION
from jobradar.matching import match_job_to_cv
from jobradar.paths import DATA_DIR
from jobradar.runtime_config import PROVIDER_KEY_MAP, get_effective_model, save_env_key
from jobradar.schemas import APPLICATION_STATUSES
from jobradar.scraping import COARSE_FILTER_VERSION

# Snapshot of the original model list taken at server start (for mock-mode reset)
_ORIGINAL_AVAILABLE_MODELS: dict[str, list[str]] = {
    k: list(v) for k, v in AVAILABLE_MODELS.items()
}

load_dotenv()

logger = get_logger(__name__)

# ─── 测试模式开关（--mock：使用 data/jobradar_test_cache.db，所有 API 调用真实发生） ──

MOCK_MODE: bool = os.getenv("JOBFINDER_MOCK") == "1"
# mock 模式下需要保护的运行时 env var，不允许被 load_dotenv(override=True) 覆盖
_RUNTIME_ENV_KEYS: tuple[str, ...] = ("JOBFINDER_MOCK", "CACHE_DB_PATH") if MOCK_MODE else ()


def _reload_dotenv() -> None:
    """重新加载 .env，但保护 mock 模式的运行时 env var 不被覆盖。"""
    saved = {k: os.environ[k] for k in _RUNTIME_ENV_KEYS if k in os.environ}
    load_dotenv(override=True)
    os.environ.update(saved)

# ─── FastAPI app ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _main_loop, _email_sync_task
    _main_loop = asyncio.get_running_loop()
    db_path = os.getenv("CACHE_DB_PATH", str(DATA_DIR / "jobradar_cache.db"))
    logger.info("JobRadar server started | mock=%s | db=%s", MOCK_MODE, db_path)
    if email_sync_configured() and not MOCK_MODE:
        _email_sync_task = asyncio.create_task(_email_sync_loop())
    try:
        yield
    finally:
        if _email_sync_task:
            _email_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await _email_sync_task


app = FastAPI(title="JobRadar", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

Instrumentator(excluded_handlers=["/metrics"]).instrument(app)

# ─── SSE 进度队列 ─────────────────────────────────────────────────────────────

_progress_q: asyncio.Queue[str] = asyncio.Queue()
_search_running = False
_search_state = "idle"
_search_state_lock = threading.Lock()
_search_run_gate = threading.Event()
_search_run_gate.set()
_search_cancel = threading.Event()
_main_loop: asyncio.AbstractEventLoop | None = None
_email_sync_task: asyncio.Task | None = None
_email_reanalysis_task: asyncio.Task | None = None
_email_reanalysis_state: dict = {
    "status": "idle",
    "stage": "idle",
    "fetched": 0,
    "candidates": 0,
    "already_processed": 0,
    "scanned": 0,
    "matched": 0,
    "pages": 0,
    "job_related": 0,
    "subscription_filtered": 0,
    "unrelated": 0,
    "unknown": 0,
    "failed_messages": 0,
    "error": "",
}
_google_oauth_state: str | None = None
_google_oauth_code_verifier: str | None = None

_MODULE_STEP_MAP = {
    "CV 解析": "cv_parse",
    "Title 粗筛": "title_relevance",
    "JD 批量评估": "jd_assessment",
    "JD Evaluation": "job_evaluation",
    "JD Profile": "jd_profile",
    "JD CV Matching": "matching",
    "Interview Prep": "interview_prep",
    "Cover Letter": "cover_letter",
    "CV Optimization": "cv_optimization",
}


async def _email_sync_loop() -> None:
    interval = max(60, int(os.getenv("EMAIL_SYNC_INTERVAL_SECONDS", "900")))
    while True:
        await asyncio.sleep(interval)
        try:
            result = await asyncio.to_thread(sync_email, trigger="scheduled")
            logger.info(
                "Scheduled email sync | mode=%s pages=%s scanned=%s matched=%s "
                "subscriptions=%s failed=%s",
                result.get("sync_mode"), result["pages"], result["scanned"], result["matched"],
                result["subscription_filtered"], result["failed_messages"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Scheduled email sync failed: %s", exc)


def _emit(event_type: str, **kwargs) -> None:
    """从同步线程安全地把 SSE 事件放入队列。"""
    payload = json.dumps({"type": event_type, **kwargs}, ensure_ascii=False)
    if _main_loop and not _main_loop.is_closed():
        _main_loop.call_soon_threadsafe(_progress_q.put_nowait, payload)


def _collect_module_metrics(pipeline_stats=None) -> dict:
    from jobradar.telemetry import telemetry

    raw = telemetry.summarize_llm_by_step()
    metrics: dict[str, dict] = {}
    for step, data in raw.items():
        key = _MODULE_STEP_MAP.get(step, step.lower().replace(" ", "_"))
        metrics[key] = {
            "step": step,
            "calls": int(data.get("calls", 0)),
            "input_tokens": int(data.get("input_tokens", 0)),
            "output_tokens": int(data.get("output_tokens", 0)),
            "elapsed": round(float(data.get("elapsed", 0.0)), 3),
            "provider": data.get("provider", ""),
            "model": data.get("model", ""),
        }

    if pipeline_stats is not None:
        title_gate = metrics.setdefault(
            "title_relevance",
            {"step": "Title 粗筛", "calls": 0, "input_tokens": 0, "output_tokens": 0, "elapsed": 0.0, "provider": "", "model": ""},
        )
        title_gate["processed"] = int(getattr(pipeline_stats, "title_relevance_in", 0))
        title_gate["rejected"] = int(getattr(pipeline_stats, "title_relevance_rejected", 0))
        title_gate["kept"] = max(0, int(title_gate["processed"]) - int(title_gate["rejected"]))

        jd_assessment = metrics.setdefault(
            "jd_assessment",
            {"step": "JD 批量评估", "calls": 0, "input_tokens": 0, "output_tokens": 0, "elapsed": 0.0, "provider": "", "model": ""},
        )
        jd_assessment["processed"] = int(getattr(pipeline_stats, "llm_assessed", 0))
        jd_assessment["rejected"] = int(getattr(pipeline_stats, "llm_rejected", 0))
        jd_assessment["kept"] = max(0, int(jd_assessment["processed"]) - int(jd_assessment["rejected"]))

        metrics["search_pipeline"] = {
            "step": "搜索评估流水线",
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "elapsed": round(float(getattr(pipeline_stats, "pipeline_elapsed", 0.0)), 3),
            "provider": "",
            "model": "",
            "scrape_elapsed": round(float(getattr(pipeline_stats, "scrape_elapsed", 0.0)), 3),
            "assessment_elapsed": round(float(getattr(pipeline_stats, "assessment_elapsed", 0.0)), 3),
            "overlap_elapsed": round(float(getattr(pipeline_stats, "overlap_elapsed", 0.0)), 3),
            "time_to_first_job": getattr(pipeline_stats, "time_to_first_job", None),
            "persistence_elapsed": round(float(getattr(pipeline_stats, "persistence_elapsed", 0.0)), 3),
            "assessment_batches": int(getattr(pipeline_stats, "assessment_batches", 0)),
            "assessment_batch_jobs": int(getattr(pipeline_stats, "assessment_batch_jobs", 0)),
            "queue_peak": int(getattr(pipeline_stats, "queue_peak", 0)),
            "queue_wait_avg": round(float(getattr(pipeline_stats, "queue_wait_avg", 0.0)), 3),
            "queue_wait_p50": round(float(getattr(pipeline_stats, "queue_wait_p50", 0.0)), 3),
            "queue_wait_p95": round(float(getattr(pipeline_stats, "queue_wait_p95", 0.0)), 3),
            "gate_workers": int(getattr(pipeline_stats, "gate_workers", 1)),
            "evaluation_commit_elapsed": round(
                float(getattr(pipeline_stats, "evaluation_commit_elapsed", 0.0)),
                3,
            ),
        }

    total_in = sum(int(item.get("input_tokens", 0)) for item in metrics.values())
    total_out = sum(int(item.get("output_tokens", 0)) for item in metrics.values())
    total_calls = sum(int(item.get("calls", 0)) for item in metrics.values())
    metrics["_summary"] = {"calls": total_calls, "input_tokens": total_in, "output_tokens": total_out}
    return metrics


# ─── Provider / Config API ─────────────────────────────────────────────────────


@app.get("/api/config")
def get_config() -> dict:
    _reload_dotenv()
    providers_status: dict[str, dict] = {}
    for provider, key_env in PROVIDER_KEY_MAP.items():
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
    "LLAMACPP_BASE_URL", "LLAMACPP_API_KEY", "LOCAL_LLM_BASE_URL",
    "DEFAULT_PROVIDER", "DEFAULT_MODEL",
    "JOB_TTL_DAYS", "SESSION_TTL_HOURS",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "EMAIL_SYNC_INTERVAL_SECONDS", "EMAIL_SYNC_MAX_MESSAGES",
}

@app.post("/api/config")
def save_config(req: ConfigSaveRequest) -> dict:
    if req.key not in _ALLOWED_ENV_KEYS:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"不允许设置的配置项：{req.key}")
    save_env_key(req.key, req.value)
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
    model_used = get_effective_model(req.provider, req.model).strip()
    return {"ok": ok, "message": msg, "model": model_used}


# ─── 职位 API ─────────────────────────────────────────────────────────────────

def _job_to_dict(j) -> dict:
    d = j.model_dump(mode="json")
    if j.match_score:
        d["match_score"] = j.match_score.model_dump(mode="json")
        d["overall_score"] = j.match_score.overall_score
        d["recommendation"] = j.match_score.recommendation
    if j.effective_score is None:
        d["score"] = None
    elif j.match_score:
        d["score"] = round(j.effective_score)
    else:
        d["score"] = round(j.effective_score * 10)
    d["strengths"] = j.effective_strengths
    d["weaknesses"] = j.effective_weaknesses
    d["matched_keywords"] = j.effective_keywords
    if j.coarse_filter:
        d["coarse_priority"] = j.coarse_filter.priority
        d["coarse_reason"] = j.coarse_filter.reason
    if j.jd_profile:
        legacy_summary = j.jd_profile.model_dump(mode="json")
        legacy_summary["must_have"] = list(j.jd_profile.must_have_requirements)
        legacy_summary["good_to_have"] = list(j.jd_profile.preferred_skills)
        d["jd_profile"] = j.jd_profile.model_dump(mode="json")
        d["job_summary"] = legacy_summary
        d["work_mode"] = j.jd_profile.work_mode
        d["job_type"] = j.jd_profile.job_type
        d["years_required"] = j.jd_profile.years_required
        d["seniority_conflict"] = j.jd_profile.seniority_conflict
    return d


class ApplicationUpdateRequest(BaseModel):
    status: str
    company: str
    job_title: str


@app.get("/api/email/status")
def get_email_status() -> dict:
    state = application_store.get_sync_state()
    return {
        "configured": google_oauth_configured(),
        "connected": email_sync_configured(),
        "interval_seconds": int(os.getenv("EMAIL_SYNC_INTERVAL_SECONDS", "900")),
        "latest_sync": application_store.latest_sync_run(),
        "paused": bool(state["paused"]),
        "history_enabled": bool(state["history_id"]),
    }


@app.post("/api/email/google/connect")
def connect_google_email(request: Request) -> dict:
    global _google_oauth_state, _google_oauth_code_verifier
    if not google_oauth_configured():
        raise HTTPException(status_code=400, detail="Google OAuth client is not configured")
    redirect_uri = str(request.url_for("google_email_callback"))
    authorization_url, _google_oauth_state, _google_oauth_code_verifier = google_oauth_authorization_url(redirect_uri)
    return {"authorization_url": authorization_url}


@app.get("/api/email/google/callback", response_class=HTMLResponse)
async def google_email_callback(request: Request, state: str, code: str) -> HTMLResponse:
    global _email_sync_task, _google_oauth_state, _google_oauth_code_verifier
    if (
        not _google_oauth_state
        or state != _google_oauth_state
        or not _google_oauth_code_verifier
    ):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    redirect_uri = str(request.url_for("google_email_callback"))
    try:
        complete_google_oauth(
            code=code,
            redirect_uri=redirect_uri,
            expected_state=_google_oauth_state,
            code_verifier=_google_oauth_code_verifier,
        )
    except Exception as exc:
        logger.exception("Google OAuth callback failed: %s", type(exc).__name__)
        return HTMLResponse(
            "<html><body><p>Google authorization failed. Close this window and try again.</p></body></html>",
            status_code=502,
        )
    _google_oauth_state = None
    _google_oauth_code_verifier = None
    if _email_sync_task is None or _email_sync_task.done():
        _email_sync_task = asyncio.create_task(_email_sync_loop())
    return HTMLResponse(
        "<html><body><p>Google email connected. You can close this window.</p>"
        "<script>window.opener && window.opener.postMessage('google-email-connected', location.origin);"
        "window.close();</script></body></html>"
    )


@app.delete("/api/email/google")
def disconnect_google_email_endpoint() -> dict:
    global _email_sync_task
    disconnect_google_email()
    if _email_sync_task:
        _email_sync_task.cancel()
        _email_sync_task = None
    return {"ok": True}


@app.post("/api/email/sync")
async def run_email_sync() -> dict:
    try:
        return await asyncio.to_thread(sync_email, trigger="manual")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/email/pause")
def pause_email_sync() -> dict:
    state = set_automatic_sync_paused(True)
    return {"ok": True, "paused": bool(state["paused"])}


@app.post("/api/email/resume")
def resume_email_sync() -> dict:
    state = set_automatic_sync_paused(False)
    return {"ok": True, "paused": bool(state["paused"])}


@app.delete("/api/email/data")
def clear_email_data(pause: bool = True) -> dict:
    try:
        clear_email_tracking_data(pause=pause)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "paused": pause}


async def _run_email_reanalysis() -> None:
    global _email_reanalysis_state

    def update_progress(progress: dict) -> None:
        global _email_reanalysis_state
        _email_reanalysis_state = {
            **_email_reanalysis_state,
            **progress,
            "status": "running",
        }

    try:
        result = await asyncio.to_thread(reanalyse_email, update_progress)
        _email_reanalysis_state = {
            **_email_reanalysis_state,
            **result,
            "status": "completed" if result["ok"] else "failed",
            "stage": "completed" if result["ok"] else "failed",
            "error": "" if result["ok"] else str(result["message"]),
        }
    except Exception as exc:
        logger.exception("Email reanalysis failed")
        _email_reanalysis_state = {
            **_email_reanalysis_state,
            "status": "failed",
            "stage": "failed",
            "error": str(exc),
        }


@app.post("/api/email/reanalyse", status_code=202)
async def reanalyse_email_data() -> dict:
    global _email_reanalysis_task, _email_reanalysis_state
    if _email_reanalysis_task is not None and not _email_reanalysis_task.done():
        raise HTTPException(status_code=409, detail="Email reanalysis is already running")
    _email_reanalysis_state = {
        "status": "running",
        "stage": "starting",
        "fetched": 0,
        "candidates": 0,
        "already_processed": 0,
        "scanned": 0,
        "matched": 0,
        "pages": 0,
        "job_related": 0,
        "subscription_filtered": 0,
        "unrelated": 0,
        "unknown": 0,
        "failed_messages": 0,
        "error": "",
    }
    _email_reanalysis_task = asyncio.create_task(_run_email_reanalysis())
    return dict(_email_reanalysis_state)


@app.get("/api/email/reanalyse/progress")
def get_email_reanalysis_progress() -> dict:
    return dict(_email_reanalysis_state)


@app.get("/api/email/sync-history")
def get_email_sync_history(limit: int = 20) -> list[dict]:
    return application_store.list_sync_runs(limit)


@app.get("/api/email/classification-metrics")
def get_email_classification_metrics() -> dict:
    return application_store.get_classification_metrics()

@app.get("/api/applications")
def get_applications(status: str | None = None) -> list[dict]:
    if status and status not in APPLICATION_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid application status")
    return [item.model_dump(mode="json") for item in application_store.list_applications(status)]


@app.get("/api/applications/{application_id}")
def get_application(application_id: int) -> dict:
    item = application_store.get_application(application_id)
    if not item:
        raise HTTPException(status_code=404, detail="Application not found")
    return item.model_dump(mode="json")


@app.get("/api/applications/{application_id}/email")
def open_application_email(application_id: int) -> RedirectResponse:
    item = application_store.get_application(application_id)
    if not item:
        raise HTTPException(status_code=404, detail="Application not found")
    event = next(
        (
            event
            for event in item.events
            if event.email_message_id and not event.email_message_id.startswith("manual:")
        ),
        None,
    )
    if not event:
        raise HTTPException(status_code=404, detail="No source email is available")
    try:
        url = gmail_thread_url(event.gmail_thread_id) if event.gmail_thread_id else gmail_message_url(
            event.email_message_id
        )
        return RedirectResponse(url, status_code=302)
    except Exception as exc:
        logger.exception("Could not open Gmail message for application %s", application_id)
        raise HTTPException(status_code=502, detail="Could not resolve the Gmail message") from exc

@app.patch("/api/applications/{application_id}")
def update_application(application_id: int, req: ApplicationUpdateRequest) -> dict:
    if req.status not in APPLICATION_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid application status")
    if not req.company.strip() or not req.job_title.strip():
        raise HTTPException(status_code=400, detail="Company and job title are required")
    item = application_store.update_application(
        application_id, status=req.status, company=req.company, job_title=req.job_title,
    )
    if not item:
        raise HTTPException(status_code=404, detail="Application not found")
    return item.model_dump(mode="json")


@app.delete("/api/applications/{application_id}")
def delete_application(application_id: int) -> dict:
    if not application_store.delete_application(application_id):
        raise HTTPException(status_code=404, detail="Application not found")
    return {"ok": True}

@app.get("/api/jobs")
def get_jobs(limit: int = 200, language: str = "zh") -> list[dict]:
    require_match = bool(cache.get_latest_cv_hash())
    jobs = cache.get_recent_jobs(limit, language=language, require_match=require_match)
    if require_match:
        jobs = [j for j in jobs if j.match_score is not None]
    jobs = [j for j in jobs if j.is_effectively_relevant]
    jobs.sort(key=lambda j: (j.effective_score if j.effective_score is not None else -1), reverse=True)
    return [_job_to_dict(j) for j in jobs]


@app.get("/api/jobs/{dedup_key:path}/artifacts")
def get_job_artifacts_endpoint(dedup_key: str, cv_hash: str = "") -> dict:
    job = cache.get_job(dedup_key)
    if job is None:
        raise HTTPException(status_code=404, detail="职位不存在或已过期。")
    resolved_cv_hash = cv_hash or cache.get_latest_cv_hash()
    if not resolved_cv_hash:
        return {
            "job_id": dedup_key,
            "cv_hash": "",
            "artifacts": {
                "interview_prep": {"exists": False, "stale": False, "updated_at": None, "data": None},
                "cover_letter": {"exists": False, "stale": False, "updated_at": None, "data": None},
                "cv_optimization": {"exists": False, "stale": False, "updated_at": None, "data": None},
            },
        }
    return {
        "job_id": dedup_key,
        "cv_hash": resolved_cv_hash,
        "artifacts": cache.get_job_artifacts(dedup_key, resolved_cv_hash, job.description_snippet),
    }


class ArtifactRequest(BaseModel):
    cv_hash: str = ""
    provider: str = "gemini"
    model: str = ""


def _resolve_artifact_context(dedup_key: str, req: ArtifactRequest):
    job = cache.get_job(dedup_key)
    if job is None:
        raise HTTPException(status_code=404, detail="职位不存在或已过期。")

    cv_hash = req.cv_hash or cache.get_latest_cv_hash()
    profile = cache.get_cv_profile(cv_hash) if cv_hash else None
    if profile is None:
        profile = cache.get_latest_cv_profile()
        if profile is not None and not cv_hash:
            cv_hash = cache.get_latest_cv_hash()
    if profile is None or not cv_hash:
        raise HTTPException(status_code=400, detail="找不到 CV 数据，请先上传 CV。")

    _model = get_effective_model(req.provider, req.model)
    llm = LLMConfig(provider=req.provider, model=_model)
    jd_profile = job.jd_profile or extract_jd_profile(job, llm)
    match = match_job_to_cv(profile, jd_profile, job.description_snippet, llm, cv_hash=cv_hash)
    return job, profile, cv_hash, llm, jd_profile, match


def _run_artifact_endpoint(
    dedup_key: str,
    req: ArtifactRequest,
    artifact_name: str,
    response_key: str,
    generator: Callable,
) -> dict:
    from jobradar.telemetry import telemetry

    try:
        telemetry.reset()
        job, profile, cv_hash, llm, jd_profile, match = _resolve_artifact_context(dedup_key, req)
        artifact = generator(profile, cv_hash, job, jd_profile, match, llm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("%s failed | job=%s error=%s", artifact_name, dedup_key, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "job_id": dedup_key,
        response_key: artifact.model_dump(mode="json"),
        "module_metrics": _collect_module_metrics(),
    }


@app.post("/api/jobs/{dedup_key:path}/interview-prep")
def create_interview_prep(dedup_key: str, req: ArtifactRequest) -> dict:
    return _run_artifact_endpoint(
        dedup_key,
        req,
        artifact_name="Interview prep",
        response_key="prep",
        generator=generate_interview_prep,
    )


@app.post("/api/jobs/{dedup_key:path}/cover-letter")
def create_cover_letter(dedup_key: str, req: ArtifactRequest) -> dict:
    return _run_artifact_endpoint(
        dedup_key,
        req,
        artifact_name="Cover letter",
        response_key="letter",
        generator=generate_cover_letter,
    )


@app.post("/api/jobs/{dedup_key:path}/cv-optimization")
def create_cv_optimization(dedup_key: str, req: ArtifactRequest) -> dict:
    return _run_artifact_endpoint(
        dedup_key,
        req,
        artifact_name="CV optimization",
        response_key="optimization",
        generator=generate_cv_optimization,
    )


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
    key_names = {v for v in PROVIDER_KEY_MAP.values() if v}
    cleared: list[str] = []
    for key in key_names:
        if os.getenv(key):
            save_env_key(key, "")
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
    from jobradar.model_fetcher import fetch_all
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
        from jobradar.telemetry import telemetry

        telemetry.reset()
        cv_text = read_cv(tmp_path)
        _model = get_effective_model(provider, model)
        llm = LLMConfig(provider=provider, model=_model)
        logger.info("CV parse started | file=%s provider=%s model=%s", file.filename, provider, _model)
        profile = extract_cv_profile(cv_text, llm=llm)
        cv_hash = hashlib.sha256(cv_text.encode()).hexdigest()
        logger.info("CV parse done | hash=%s seniority=%s skills=%d", cv_hash[:8], profile.seniority_display, len(profile.skills))
        return {"cv_hash": cv_hash, "profile": profile.model_dump(mode="json"), "module_metrics": _collect_module_metrics()}
    except Exception as e:
        logger.error("CV parse failed | file=%s error=%s", file.filename, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── 搜索 API ─────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    cv_hash: str
    roles: list[str]
    location: str
    experiment_name: str = ""
    notes: str = ""
    provider: str = "gemini"
    model: str = ""
    refresh: bool = False
    language: str = "zh"
    limit_per_role: int = 100
    linkedin_limit_per_role: int = 30
    hours_old: int | None = 72


def _set_search_state(state: str) -> None:
    global _search_running, _search_state
    with _search_state_lock:
        _search_state = state
        _search_running = state != "idle"


def _transition_search_state(expected: set[str], state: str) -> bool:
    global _search_running, _search_state
    with _search_state_lock:
        if _search_state not in expected:
            return False
        _search_state = state
        _search_running = state != "idle"
        return True


def _search_checkpoint() -> None:
    from jobradar.agent import SearchCancelled

    while not _search_run_gate.wait(timeout=0.2):
        if _search_cancel.is_set():
            raise SearchCancelled()
    if _search_cancel.is_set():
        raise SearchCancelled()


@app.post("/api/search")
async def start_search(req: SearchRequest, background_tasks: BackgroundTasks) -> dict:
    if not _transition_search_state({"idle"}, "running"):
        raise HTTPException(status_code=409, detail="搜索正在进行中，请等待完成。")
    _search_cancel.clear()
    _search_run_gate.set()
    background_tasks.add_task(_run_search_task, req)
    return {"status": "started"}


@app.post("/api/search/pause")
def pause_search() -> dict:
    if not _transition_search_state({"running"}, "paused"):
        raise HTTPException(status_code=409, detail="Only a running search can be paused.")
    _search_run_gate.clear()
    _emit("state", state="paused")
    logger.info("Search paused by user")
    return {"status": "paused"}


@app.post("/api/search/resume")
def resume_search() -> dict:
    if not _transition_search_state({"paused"}, "running"):
        raise HTTPException(status_code=409, detail="Only a paused search can be resumed.")
    _search_run_gate.set()
    _emit("state", state="running")
    logger.info("Search resumed by user")
    return {"status": "running"}


@app.post("/api/search/stop")
def stop_search() -> dict:
    if not _transition_search_state({"running", "paused"}, "stopping"):
        raise HTTPException(status_code=409, detail="No active search can be stopped.")
    _search_cancel.set()
    _search_run_gate.set()
    _emit("state", state="stopping")
    logger.info("Search stop requested by user")
    return {"status": "stopping"}


async def _run_search_task(req: SearchRequest) -> None:
    """在后台线程运行真实搜索，通过 on_job 逐条 emit 职位到前端。"""
    def on_job(key: str) -> None:
        job = cache.get_job(key, language=req.language)
        if job:
            _emit("job", job=_job_to_dict(job))

    def run() -> None:
        import time as _time

        from jobradar.telemetry import telemetry
        try:
            from jobradar.agent import SearchCancelled, run_search

            profile = cache.get_cv_profile(req.cv_hash)
            if profile is None:
                logger.warning("cv_hash %s not found, falling back to latest profile", req.cv_hash[:8])
                profile = cache.get_latest_cv_profile()
            if profile is None:
                logger.error("Search failed: no CV profile in DB (cv_hash=%s)", req.cv_hash[:8])
                _emit("error", msg="CV profile not found. Please upload your CV first.")
                return

            profile = profile.model_copy(update={"preferred_roles": req.roles})

            _model = get_effective_model(req.provider, req.model)
            llm = LLMConfig(provider=req.provider, model=_model)
            logger.info(
                "Search started | location=%s roles=%s provider=%s model=%s refresh=%s experiment=%s",
                req.location, req.roles, req.provider, _model, req.refresh, req.experiment_name,
            )

            telemetry.reset()
            _search_start = _time.monotonic()

            dedup_keys, pipeline_stats = run_search(
                profile=profile,
                location=req.location,
                llm=llm,
                cv_hash=req.cv_hash,
                on_progress=lambda msg: _emit("progress", msg=msg),
                on_job=on_job,
                force_refresh=req.refresh,
                language=req.language,
                limit_per_role=req.limit_per_role,
                linkedin_limit_per_role=req.linkedin_limit_per_role,
                hours_old=req.hours_old,
                control_checkpoint=_search_checkpoint,
            )

            elapsed = _time.monotonic() - _search_start
            with telemetry._lock:
                tokens_in  = sum(r.input_tokens  for r in telemetry.llm_records)
                tokens_out = sum(r.output_tokens for r in telemetry.llm_records)

            logger.info(
                "Search done | jobs=%d elapsed=%.1fs tokens_in=%d tokens_out=%d",
                len(dedup_keys), elapsed, tokens_in, tokens_out,
            )
            funnel_data = pipeline_stats.to_dict()
            module_metrics = _collect_module_metrics(pipeline_stats)
            cache.save_search_stats(
                run_id=getattr(pipeline_stats, "run_id", ""),
                experiment_name=req.experiment_name,
                notes=req.notes,
                location=req.location,
                roles=req.roles,
                provider=req.provider,
                model=_model,
                elapsed=elapsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                jobs_found=len(dedup_keys),
                scraped_total=funnel_data.get("scraped_total", 0),
                deduped_total=max(0, int(funnel_data.get("prefilter_in", 0)) - int(funnel_data.get("skip_dup", 0))),
                filtered_total=len(dedup_keys),
                new_jobs=int(funnel_data.get("new_saved", 0)),
                funnel=funnel_data,
                cv_hash=req.cv_hash,
                app_version=__version__,
                cv_prompt_version=CV_PROMPT_VERSION,
                jd_summary_prompt_version=JD_PROFILE_PROMPT_VERSION,
                match_prompt_version=MATCH_PROMPT_VERSION,
                title_relevance_prompt_version=TITLE_RELEVANCE_PROMPT_VERSION,
                title_gate_version=TITLE_GATE_VERSION,
                coarse_filter_version=COARSE_FILTER_VERSION,
                module_metrics=module_metrics,
            )
            try:
                dedup_report = run_dedup_check(list(dedup_keys))
            except Exception as _de:
                logger.warning("Dedup check failed: %s", _de)
                dedup_report = {"total": 0, "l1": 0, "l2": 0, "l2_items": []}
            _emit("done", count=len(dedup_keys),
                  elapsed=round(elapsed, 1),
                  tokens_in=tokens_in, tokens_out=tokens_out,
                  pipeline_stats=funnel_data,
                  module_metrics=module_metrics,
                  dedup=dedup_report)
        except SearchCancelled:
            logger.info("Search stopped by user")
            _emit("stopped")
        except Exception as e:
            logger.error("Search failed | error=%s", e, exc_info=True)
            _emit("error", msg=str(e))
        finally:
            _search_cancel.clear()
            _search_run_gate.set()
            _set_search_state("idle")

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
            if data.get("type") in ("done", "error", "stopped", "timeout"):
                break

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/search/status")
def search_status() -> dict:
    with _search_state_lock:
        state = _search_state
    return {"running": state != "idle", "state": state, "mock_mode": MOCK_MODE}


# ─── 统计 API ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(limit: int = 50) -> dict:
    """返回搜索历史记录和全量累计统计。"""
    return {
        "records": cache.get_search_stats(limit=limit),
        "summary": cache.get_stats_summary(),
        "benchmark": cache.get_benchmark_summary(limit=limit),
    }


@app.get("/api/filter-events")
def get_filter_events(run_id: str = "", limit: int = 500) -> dict:
    return {
        "run_id": run_id,
        "events": cache.get_filter_events(run_id=run_id, limit=limit),
    }


@app.delete("/api/stats")
def delete_stats() -> dict:
    """清空全部搜索历史记录。"""
    cache.clear_search_stats()
    return {"ok": True}


# ─── Prometheus 指标 ─────────────────────────────────────────────────────────

@app.get("/metrics")
def metrics() -> PlainTextResponse:
    """以 Prometheus 文本格式暴露指标，供 Prometheus 抓取。

    输出由两个来源拼接而成，两者语义不同，刻意不统一：

    1. prometheus_client 全局注册表（generate_latest）——进程内实时采集，
       包含 email sync 埋点与默认的 python_gc_* / process_*（后者仅 Linux）。
       进程重启即归零，这是 counter 的正常语义，PromQL 的 rate() 能识别
       counter reset 并正确计算。

    2. cache.get_stats_summary()——SQLite 中跨进程持久化的历史累计值，
       重启后依然存在，故手工拼接文本而非改用 Counter：Counter 是进程内的，
       用它承载持久化累计值属于语义错配。
    """
    s = cache.get_stats_summary()
    business_lines = [
        "# HELP jobradar_searches_total Total searches run",
        "# TYPE jobradar_searches_total counter",
        f"jobradar_searches_total {s['total_searches']}",

        "# HELP jobradar_jobs_found_total Total jobs found across all searches",
        "# TYPE jobradar_jobs_found_total counter",
        f"jobradar_jobs_found_total {s['total_jobs']}",

        "# HELP jobradar_llm_tokens_total Total LLM tokens consumed",
        "# TYPE jobradar_llm_tokens_total counter",
        f'jobradar_llm_tokens_total{{direction="in"}} {s["total_tokens_in"]}',
        f'jobradar_llm_tokens_total{{direction="out"}} {s["total_tokens_out"]}',

        "# HELP jobradar_search_seconds_total Cumulative time spent searching",
        "# TYPE jobradar_search_seconds_total counter",
        f"jobradar_search_seconds_total {s['total_elapsed']}",
    ]
    registry_text = generate_latest().decode("utf-8")
    body = registry_text + "\n" + "\n".join(business_lines) + "\n"
    return PlainTextResponse(body, media_type=CONTENT_TYPE_LATEST)


# ─── 日志 API ────────────────────────────────────────────────────────────────

@app.get("/api/logs")
def get_logs(lines: int = 200, level: str = "") -> dict:
    """返回日志文件最后 N 行，可按 level 过滤（ERROR/WARNING/INFO/DEBUG）。"""
    from jobradar.logger import _LOG_FILE
    log_path = Path(_LOG_FILE) if _LOG_FILE else None
    if not log_path or not log_path.exists():
        return {"lines": [], "path": str(log_path or "disabled")}

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        if level:
            lvl = level.upper()
            all_lines = [line for line in all_lines if f"[{lvl}]" in line]
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
