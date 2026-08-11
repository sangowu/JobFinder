"""SQLite 缓存层：JobResult / SearchSession / FailedURL 三张表。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from jobradar import artifact_store
from jobradar.paths import DATA_DIR, ensure_parent
from jobradar.schemas import (
    CoarseFilterResult,
    CoverLetter,
    CVOptimization,
    CVProfile,
    InterviewPrep,
    JDProfile,
    JobAssessment,
    JobResult,
    JobSummary,
    MatchScore,
    SearchSession,
    make_dedup_key,
)

_DEFAULT_DB_PATH = str(DATA_DIR / "jobradar_cache.db")

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS job_cache (
    dedup_key           TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    location            TEXT,
    description_snippet TEXT,
    url                 TEXT,
    sources             TEXT,       -- JSON array of source names
    raw_sources         TEXT NOT NULL DEFAULT '[]',  -- JSON array of {source, url, date_posted}
    fetched_at          TEXT NOT NULL,
    expires_at          TEXT,       -- NULL 表示无截止日期
    is_complete         INTEGER NOT NULL DEFAULT 1,
    coarse_filter       TEXT,       -- JSON: CoarseFilterResult
    assessment          TEXT        -- JSON: {score, strengths, weaknesses}，NULL 表示未评估
);

CREATE TABLE IF NOT EXISTS search_sessions (
    session_key         TEXT PRIMARY KEY,
    roles               TEXT NOT NULL,  -- JSON array
    location            TEXT NOT NULL,
    seniority           TEXT NOT NULL,
    search_language     TEXT NOT NULL,
    job_dedup_keys      TEXT NOT NULL,  -- JSON array
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failed_urls (
    url         TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    skipped_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cv_cache (
    cv_hash     TEXT PRIMARY KEY,  -- SHA-256(cv_text)
    profile_json TEXT NOT NULL,    -- CVProfile JSON
    prompt_version TEXT NOT NULL DEFAULT '',
    cached_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS url_visits (
    url         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,   -- fetched / empty / error / verification_failed
    visited_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    run_id      TEXT NOT NULL DEFAULT '',
    experiment_name TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    roles       TEXT NOT NULL DEFAULT '[]',  -- JSON array
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    elapsed     REAL NOT NULL DEFAULT 0,     -- 秒
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    jobs_found  INTEGER NOT NULL DEFAULT 0,
    scraped_total INTEGER NOT NULL DEFAULT 0,
    deduped_total INTEGER NOT NULL DEFAULT 0,
    filtered_total INTEGER NOT NULL DEFAULT 0,
    new_jobs    INTEGER NOT NULL DEFAULT 0,
    cv_hash     TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    cv_prompt_version TEXT NOT NULL DEFAULT '',
    jd_summary_prompt_version TEXT NOT NULL DEFAULT '',
    match_prompt_version TEXT NOT NULL DEFAULT '',
    title_relevance_prompt_version TEXT NOT NULL DEFAULT '',
    title_gate_version TEXT NOT NULL DEFAULT '',
    coarse_filter_version TEXT NOT NULL DEFAULT '',
    module_metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS filter_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    run_id      TEXT NOT NULL DEFAULT '',
    stage       TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS search_candidates (
    run_id          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    candidate_json  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (run_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS job_summaries (
    job_id            TEXT PRIMARY KEY,
    description_hash  TEXT NOT NULL,
    summary_json      TEXT NOT NULL,
    model_name        TEXT NOT NULL DEFAULT '',
    prompt_version    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jd_profiles (
    job_id            TEXT PRIMARY KEY,
    description_hash  TEXT NOT NULL,
    profile_json      TEXT NOT NULL,
    model_name        TEXT NOT NULL DEFAULT '',
    prompt_version    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_matches (
    job_id            TEXT NOT NULL,
    cv_hash           TEXT NOT NULL,
    description_hash  TEXT NOT NULL,
    overall_score     REAL NOT NULL DEFAULT 0,
    recommendation    TEXT NOT NULL DEFAULT 'skip',
    score_json        TEXT NOT NULL,
    model_name        TEXT NOT NULL DEFAULT '',
    prompt_version    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (job_id, cv_hash)
);

CREATE TABLE IF NOT EXISTS job_relevance_rejections (
    job_id            TEXT NOT NULL,
    cv_hash           TEXT NOT NULL,
    description_hash  TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    score             INTEGER NOT NULL DEFAULT 0,
    model_name        TEXT NOT NULL DEFAULT '',
    prompt_version    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (job_id, cv_hash)
);

CREATE TABLE IF NOT EXISTS interview_preps (
    job_id            TEXT NOT NULL,
    cv_hash           TEXT NOT NULL,
    description_hash  TEXT NOT NULL,
    prep_json         TEXT NOT NULL,
    model_name        TEXT NOT NULL DEFAULT '',
    prompt_version    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (job_id, cv_hash)
);

CREATE TABLE IF NOT EXISTS cover_letters (
    job_id            TEXT NOT NULL,
    cv_hash           TEXT NOT NULL,
    description_hash  TEXT NOT NULL,
    letter_json       TEXT NOT NULL,
    model_name        TEXT NOT NULL DEFAULT '',
    prompt_version    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (job_id, cv_hash)
);

CREATE TABLE IF NOT EXISTS cv_optimizations (
    job_id            TEXT NOT NULL,
    cv_hash           TEXT NOT NULL,
    description_hash  TEXT NOT NULL,
    optimization_json TEXT NOT NULL,
    model_name        TEXT NOT NULL DEFAULT '',
    prompt_version    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (job_id, cv_hash)
);
"""

# 迁移语句：对已存在的旧表补加列（IF NOT EXISTS 语法 SQLite ≥ 3.37 支持）
_MIGRATE_SQL = """
ALTER TABLE job_cache ADD COLUMN assessment TEXT;
ALTER TABLE search_stats ADD COLUMN cv_hash TEXT NOT NULL DEFAULT '';
"""

_INIT_LOCK = threading.Lock()
_INITIALIZED_DB_PATHS: set[str] = set()


def _initialize_connection(con: sqlite3.Connection, db_path: str) -> None:
    con.execute("PRAGMA busy_timeout = 30000")
    if db_path in _INITIALIZED_DB_PATHS:
        return

    with _INIT_LOCK:
        if db_path in _INITIALIZED_DB_PATHS:
            return
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(_INIT_SQL)
        # 迁移：旧库补加字段（字段已存在时 SQLite 会报错，忽略即可）。
        for migration in (
            "ALTER TABLE job_cache ADD COLUMN assessment TEXT",
            "ALTER TABLE job_cache ADD COLUMN coarse_filter TEXT",
            "ALTER TABLE job_cache ADD COLUMN company_profile TEXT",  # deprecated, kept for old DB compat
            "ALTER TABLE job_cache ADD COLUMN date_posted TEXT DEFAULT ''",
            "ALTER TABLE job_cache ADD COLUMN raw_sources TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE search_stats ADD COLUMN funnel_json TEXT",
            "ALTER TABLE search_stats ADD COLUMN run_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN cv_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN experiment_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN notes TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN scraped_total INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE search_stats ADD COLUMN deduped_total INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE search_stats ADD COLUMN filtered_total INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE search_stats ADD COLUMN new_jobs INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE search_stats ADD COLUMN app_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN cv_prompt_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN jd_summary_prompt_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN match_prompt_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN title_relevance_prompt_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN title_gate_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN coarse_filter_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN module_metrics_json TEXT",
            "ALTER TABLE cv_cache ADD COLUMN prompt_version TEXT NOT NULL DEFAULT ''",
        ):
            try:
                con.execute(migration)
                con.commit()
            except sqlite3.OperationalError:
                pass
        _INITIALIZED_DB_PATHS.add(db_path)


@contextmanager
def _conn():
    db_path = Path(os.getenv("CACHE_DB_PATH", _DEFAULT_DB_PATH))
    ensure_parent(db_path)
    resolved_db_path = str(db_path.resolve())
    con = sqlite3.connect(resolved_db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        _initialize_connection(con, resolved_db_path)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ─── JobResult ────────────────────────────────────────────────────────────────


def _source_provider_key(source: str, url: str = "") -> str:
    source_name = str(source or "").strip()
    try:
        host = urlparse(url).netloc.removeprefix("www.").lower()
    except ValueError:
        host = ""
    candidates = (source_name.lower(), host)
    if any("linkedin" in candidate for candidate in candidates):
        return "linkedin"
    if any("indeed" in candidate for candidate in candidates):
        return "indeed"
    return source_name.casefold() or host or "unknown"


def _source_display_name(source: str, url: str = "") -> str:
    source_name = str(source or "").strip()
    if source_name and source_name.lower() != "unknown":
        return source_name
    provider = _source_provider_key(source_name, url)
    if provider == "linkedin":
        return "linkedin.com"
    if provider == "indeed":
        return "indeed.ie"
    return source_name or "unknown"


def _deduplicate_job_sources(
    sources: list[str] | list[dict],
    raw_sources: list[dict],
) -> tuple[list[str], list[dict]]:
    """Collapse hostname aliases that represent the same job platform."""
    names_by_provider: dict[str, str] = {}
    for source in sources:
        if isinstance(source, dict):
            source_name = str(source.get("source") or "")
            source_url = str(source.get("url") or "")
        else:
            source_name = str(source or "")
            source_url = ""
        provider = _source_provider_key(source_name, source_url)
        names_by_provider.setdefault(provider, _source_display_name(source_name, source_url))

    entries_by_provider: dict[str, dict] = {}
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            continue
        source_name = str(raw_source.get("source") or "")
        source_url = str(raw_source.get("url") or "")
        provider = _source_provider_key(source_name, source_url)
        display_name = names_by_provider.setdefault(
            provider,
            _source_display_name(source_name, source_url),
        )
        entry = dict(raw_source)
        entry["source"] = display_name
        existing = entries_by_provider.get(provider)
        if existing is None:
            entries_by_provider[provider] = entry
            continue
        for field in ("url", "date_posted"):
            if not existing.get(field) and entry.get(field):
                existing[field] = entry[field]

    return list(names_by_provider.values()), list(entries_by_provider.values())


def get_job(dedup_key: str, language: str = "zh") -> JobResult | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM job_cache WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
    if row is None:
        return None
    job = _row_to_job(row)
    from jobradar.jd_profile import jd_profile_prompt_version

    job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet, prompt_version=jd_profile_prompt_version(language))
    if job.jd_profile is None:
        job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet)
    _sync_legacy_job_summary(job)
    _attach_latest_match(job, language=language)
    return job


def save_job(job: JobResult) -> None:
    """首次写入完整记录；已存在时仅追加 source 并更新 expires_at（若有）。"""
    existing = get_job(job.dedup_key)
    if existing is None:
        _insert_job(job)
    else:
        _merge_job(existing, job)


def _insert_job(job: JobResult) -> None:
    sources, raw_sources = _deduplicate_job_sources(job.sources, job.raw_sources)
    with _conn() as con:
        con.execute(
            """
            INSERT INTO job_cache
              (dedup_key, title, company, location, description_snippet,
               url, sources, raw_sources, date_posted, fetched_at, expires_at, is_complete, coarse_filter, assessment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.dedup_key,
                job.title,
                job.company,
                job.location,
                job.description_snippet,
                job.url,
                json.dumps(sources),
                json.dumps(raw_sources),
                job.date_posted,
                job.fetched_at.isoformat(),
                job.expires_at.isoformat() if job.expires_at else None,
                int(job.is_complete),
                job.coarse_filter.model_dump_json() if job.coarse_filter else None,
                job.assessment.model_dump_json() if job.assessment else None,
            ),
        )


def _merge_job(existing: JobResult, new: JobResult) -> None:
    """追加新来源；若新记录有 expires_at / assessment，则更新。"""
    merged_sources, merged_raw = _deduplicate_job_sources(
        existing.sources + new.sources,
        existing.raw_sources + new.raw_sources,
    )
    new_expires = new.expires_at or existing.expires_at
    new_coarse_filter = new.coarse_filter or existing.coarse_filter
    new_assessment = new.assessment or existing.assessment

    with _conn() as con:
        con.execute(
            """
            UPDATE job_cache
            SET sources = ?, raw_sources = ?, expires_at = ?, coarse_filter = ?, assessment = ?
            WHERE dedup_key = ?
            """,
            (
                json.dumps(merged_sources),
                json.dumps(merged_raw),
                new_expires.isoformat() if new_expires else None,
                new_coarse_filter.model_dump_json() if new_coarse_filter else None,
                new_assessment.model_dump_json() if new_assessment else None,
                existing.dedup_key,
            ),
        )


def merge_job_source(dedup_key: str, source: str) -> None:
    """将新来源追加到已存在的 job_cache 记录中（幂等）。"""
    with _conn() as con:
        row = con.execute(
            "SELECT sources FROM job_cache WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if row is None:
            return
        existing, _ = _deduplicate_job_sources(
            json.loads(row["sources"] or "[]") + [source],
            [],
        )
        if existing != json.loads(row["sources"] or "[]"):
            con.execute(
                "UPDATE job_cache SET sources = ? WHERE dedup_key = ?",
                (json.dumps(existing), dedup_key),
            )


def merge_job_raw_source(dedup_key: str, source_entry: dict) -> None:
    """将完整来源记录合并到已评估职位，供流式抓取后到达的重复来源使用。"""
    source_name = str(source_entry.get("source") or "unknown")
    with _conn() as con:
        row = con.execute(
            "SELECT sources, raw_sources FROM job_cache WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if row is None:
            return
        sources, raw_sources = _deduplicate_job_sources(
            json.loads(row["sources"] or "[]") + [source_name],
            json.loads(row["raw_sources"] or "[]") + [source_entry],
        )
        con.execute(
            "UPDATE job_cache SET sources = ?, raw_sources = ? WHERE dedup_key = ?",
            (json.dumps(sources), json.dumps(raw_sources), dedup_key),
        )


def get_recent_jobs(
    limit: int = 50,
    language: str = "zh",
    *,
    require_match: bool = False,
) -> list[JobResult]:
    """按抓取时间倒序返回最近职位，可限定为当前 CV 下已有可见匹配。"""
    with _conn() as con:
        if require_match:
            cv_hash = get_latest_cv_hash()
            if not cv_hash:
                return []
            rows = con.execute(
                """
                SELECT job_cache.*
                FROM job_cache
                JOIN job_matches
                  ON job_matches.job_id = job_cache.dedup_key
                 AND job_matches.cv_hash = ?
                 AND job_matches.recommendation != 'skip'
                ORDER BY job_cache.fetched_at DESC
                LIMIT ?
                """,
                (cv_hash, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM job_cache ORDER BY fetched_at DESC LIMIT ?", (limit,)
            ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    from jobradar.jd_profile import jd_profile_prompt_version

    for job in jobs:
        job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet, prompt_version=jd_profile_prompt_version(language))
        if job.jd_profile is None:
            job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet)
        _sync_legacy_job_summary(job)
        _attach_latest_match(job, language=language)
    return [j for j in jobs if not j.is_expired]


def get_job_by_url(url: str, language: str = "zh") -> JobResult | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM job_cache WHERE url = ?", (url,)
        ).fetchone()
    if row is None:
        return None
    job = _row_to_job(row)
    from jobradar.jd_profile import jd_profile_prompt_version

    job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet, prompt_version=jd_profile_prompt_version(language))
    if job.jd_profile is None:
        job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet)
    _sync_legacy_job_summary(job)
    _attach_latest_match(job, language=language)
    return job


def get_jobs_by_keys(dedup_keys: list[str], language: str = "zh") -> list[JobResult]:
    if not dedup_keys:
        return []
    placeholders = ",".join("?" * len(dedup_keys))
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM job_cache WHERE dedup_key IN ({placeholders})",
            dedup_keys,
        ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    from jobradar.jd_profile import jd_profile_prompt_version

    for job in jobs:
        job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet, prompt_version=jd_profile_prompt_version(language))
        if job.jd_profile is None:
            job.jd_profile = get_jd_profile(job.dedup_key, job.description_snippet)
        _sync_legacy_job_summary(job)
        _attach_latest_match(job, language=language)
    return [j for j in jobs if not j.is_expired]


def _row_to_job(row: sqlite3.Row) -> JobResult:
    keys = row.keys()
    raw_coarse_filter = row["coarse_filter"] if "coarse_filter" in keys else None
    raw_assessment = row["assessment"] if "assessment" in keys else None
    coarse_filter = CoarseFilterResult.model_validate_json(raw_coarse_filter) if raw_coarse_filter else None
    assessment = JobAssessment.model_validate_json(raw_assessment) if raw_assessment else None
    sources, raw_sources = _deduplicate_job_sources(
        json.loads(row["sources"] or "[]"),
        json.loads(row["raw_sources"] if "raw_sources" in row.keys() and row["raw_sources"] else "[]"),
    )
    return JobResult(
        title=row["title"],
        company=row["company"],
        location=row["location"] or "",
        url=row["url"],
        description_snippet=row["description_snippet"] or "",
        sources=sources,
        raw_sources=raw_sources,
        date_posted=row["date_posted"] if "date_posted" in row.keys() and row["date_posted"] else "",
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        is_complete=bool(row["is_complete"]),
        coarse_filter=coarse_filter,
        assessment=assessment,
    )


def _sync_legacy_job_summary(job: JobResult) -> None:
    if job.jd_profile is not None and job.job_summary is None:
        job.job_summary = JobSummary.model_validate(job.jd_profile.model_dump(mode="json"))


def _description_hash(description: str) -> str:
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


def get_jd_profile(
    job_id: str,
    description: str = "",
    prompt_version: str | Sequence[str] = "",
) -> JDProfile | None:
    """Read a cached JD profile.

    ``prompt_version`` accepts a sequence when several prompts produce
    interchangeable profiles (standalone extraction vs. the combined
    JD-evaluation call); the row matches if it carries any of them.
    """
    if isinstance(prompt_version, str):
        accepted = {prompt_version} if prompt_version else set()
        primary = prompt_version
    else:
        accepted = {version for version in prompt_version if version}
        primary = next((version for version in prompt_version if version), "")

    with _conn() as con:
        row = con.execute(
            "SELECT profile_json, description_hash, prompt_version FROM jd_profiles WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is not None:
        if description and row["description_hash"] != _description_hash(description):
            return None
        if accepted and row["prompt_version"] not in accepted:
            return None
        return JDProfile.model_validate_json(row["profile_json"])

    # Legacy fallback: read older job_summaries rows and upcast them.
    # Those rows predate the combined prompt, so only the standalone tag applies.
    legacy = get_job_summary(job_id, description=description, prompt_version=primary)
    if legacy is None:
        return None
    return JDProfile.model_validate(legacy.model_dump(mode="json"))


def get_jd_profile_prompt_version(job_id: str) -> str:
    """Return the prompt tag stored with a cached profile, or "" when absent."""
    with _conn() as con:
        row = con.execute(
            "SELECT prompt_version FROM jd_profiles WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return str(row["prompt_version"] or "") if row is not None else ""


def save_jd_profile(
    job_id: str,
    description: str,
    profile: JDProfile,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO jd_profiles
              (job_id, description_hash, profile_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              description_hash = excluded.description_hash,
              profile_json = excluded.profile_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                job_id,
                _description_hash(description),
                profile.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_job_summary(job_id: str, description: str = "", prompt_version: str = "") -> JobSummary | None:
    with _conn() as con:
        row = con.execute(
            "SELECT summary_json, description_hash, prompt_version FROM job_summaries WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != _description_hash(description):
        return None
    if prompt_version and row["prompt_version"] != prompt_version:
        return None
    return JobSummary.model_validate_json(row["summary_json"])


def save_job_summary(
    job_id: str,
    description: str,
    summary: JobSummary,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO job_summaries
              (job_id, description_hash, summary_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              description_hash = excluded.description_hash,
              summary_json = excluded.summary_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                job_id,
                _description_hash(description),
                summary.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_job_match(job_id: str, cv_hash: str, description: str = "", prompt_version: str = "") -> MatchScore | None:
    with _conn() as con:
        row = con.execute(
            "SELECT score_json, description_hash, prompt_version FROM job_matches WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != _description_hash(description):
        return None
    if prompt_version and row["prompt_version"] != prompt_version:
        return None
    return MatchScore.model_validate_json(row["score_json"])


def get_job_relevance_rejection(
    job_id: str,
    cv_hash: str,
    description: str = "",
    prompt_version: str = "",
) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """
            SELECT reason, score, model_name, description_hash, prompt_version
            FROM job_relevance_rejections
            WHERE job_id = ? AND cv_hash = ?
            """,
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != _description_hash(description):
        return None
    if prompt_version and row["prompt_version"] != prompt_version:
        return None
    return {
        "reason": row["reason"],
        "score": row["score"],
        "model_name": row["model_name"],
        "prompt_version": row["prompt_version"],
    }


def save_job_relevance_rejection(
    *,
    job_id: str,
    cv_hash: str,
    description: str,
    reason: str,
    score: int,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO job_relevance_rejections
              (job_id, cv_hash, description_hash, reason, score, model_name,
               prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              reason = excluded.reason,
              score = excluded.score,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                job_id,
                cv_hash,
                _description_hash(description),
                reason,
                score,
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def _attach_latest_match(job: JobResult, language: str = "zh") -> None:
    latest_cv_hash = get_latest_cv_hash()
    if not latest_cv_hash:
        return
    from jobradar.matching import cv_profile_hash, match_prompt_version

    prompt_version = match_prompt_version(language)
    match = get_job_match(job.dedup_key, latest_cv_hash, job.description_snippet, prompt_version=prompt_version)
    profile = get_cv_profile(latest_cv_hash)
    if match is None:
        match = get_job_match(job.dedup_key, latest_cv_hash, job.description_snippet)
    if match is None and profile is not None:
        legacy_hash = cv_profile_hash(profile)
        if legacy_hash != latest_cv_hash:
            match = get_job_match(job.dedup_key, legacy_hash, job.description_snippet, prompt_version=prompt_version)
            if match is None:
                match = get_job_match(job.dedup_key, legacy_hash, job.description_snippet)
    if match is None:
        return
    if profile is not None and job.jd_profile is not None:
        from jobradar.matching import adjust_match_for_profile

        match = adjust_match_for_profile(profile, job.jd_profile, match, language=language)
    job.match_score = match


def save_job_match(
    match: MatchScore,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO job_matches
              (job_id, cv_hash, description_hash, overall_score, recommendation, score_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              overall_score = excluded.overall_score,
              recommendation = excluded.recommendation,
              score_json = excluded.score_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                match.job_id,
                match.cv_hash,
                _description_hash(description),
                match.overall_score,
                match.recommendation,
                match.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def save_job_evaluation(
    profile: JDProfile,
    match: MatchScore,
    description: str,
    model_name: str = "",
    profile_prompt_version: str = "",
    match_prompt_version: str = "",
) -> None:
    """Atomically persist the profile and CV match produced for one job."""
    if profile.job_id != match.job_id:
        raise ValueError("profile and match must belong to the same job")
    now = datetime.utcnow().isoformat()
    description_hash = _description_hash(description)
    with _conn() as con:
        con.execute(
            """
            INSERT INTO jd_profiles
              (job_id, description_hash, profile_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              description_hash = excluded.description_hash,
              profile_json = excluded.profile_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                profile.job_id,
                description_hash,
                profile.model_dump_json(),
                model_name,
                profile_prompt_version,
                now,
                now,
            ),
        )
        con.execute(
            """
            INSERT INTO job_matches
              (job_id, cv_hash, description_hash, overall_score, recommendation, score_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              overall_score = excluded.overall_score,
              recommendation = excluded.recommendation,
              score_json = excluded.score_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                match.job_id,
                match.cv_hash,
                description_hash,
                match.overall_score,
                match.recommendation,
                match.model_dump_json(),
                model_name,
                match_prompt_version,
                now,
                now,
            ),
        )


def get_interview_prep(job_id: str, cv_hash: str, description: str = "") -> InterviewPrep | None:
    return artifact_store.get_interview_prep(_conn, _description_hash, job_id, cv_hash, description)


def save_interview_prep(
    prep: InterviewPrep,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    artifact_store.save_interview_prep(_conn, _description_hash, prep, description, model_name=model_name, prompt_version=prompt_version)


def get_cover_letter(job_id: str, cv_hash: str, description: str = "") -> CoverLetter | None:
    return artifact_store.get_cover_letter(_conn, _description_hash, job_id, cv_hash, description)


def save_cover_letter(
    letter: CoverLetter,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    artifact_store.save_cover_letter(_conn, _description_hash, letter, description, model_name=model_name, prompt_version=prompt_version)


def get_cv_optimization(job_id: str, cv_hash: str, description: str = "") -> CVOptimization | None:
    return artifact_store.get_cv_optimization(_conn, _description_hash, job_id, cv_hash, description)


def save_cv_optimization(
    optimization: CVOptimization,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    artifact_store.save_cv_optimization(_conn, _description_hash, optimization, description, model_name=model_name, prompt_version=prompt_version)


def get_job_artifacts(job_id: str, cv_hash: str, description: str = "") -> dict[str, dict]:
    return artifact_store.get_job_artifacts(_conn, _description_hash, job_id, cv_hash, description)


# ─── SearchSession ────────────────────────────────────────────────────────────


def get_session(session_key: str) -> SearchSession | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM search_sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
    if row is None:
        return None
    session = SearchSession(
        roles=json.loads(row["roles"]),
        location=row["location"],
        seniority=row["seniority"],
        search_language=row["search_language"],
        job_dedup_keys=json.loads(row["job_dedup_keys"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
    return None if session.is_expired else session


def save_session(session: SearchSession) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT INTO search_sessions
              (session_key, roles, location, seniority, search_language,
               job_dedup_keys, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
              job_dedup_keys = excluded.job_dedup_keys,
              created_at = excluded.created_at
            """,
            (
                session.session_key,
                json.dumps(session.roles),
                session.location,
                session.seniority,
                session.search_language,
                json.dumps(session.job_dedup_keys),
                session.created_at.isoformat(),
            ),
        )


# ─── FailedURL ────────────────────────────────────────────────────────────────


def is_failed_url(url: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM failed_urls WHERE url = ?", (url,)
        ).fetchone()
    return row is not None


def record_failed_url(url: str, reason: str) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT INTO failed_urls (url, reason, skipped_at)
            VALUES (?, ?, ?)
            ON CONFLICT(url) DO NOTHING
            """,
            (url, reason, datetime.utcnow().isoformat()),
        )


def get_failed_urls(urls: list[str]) -> set[str]:
    if not urls:
        return set()
    placeholders = ",".join("?" * len(urls))
    with _conn() as con:
        rows = con.execute(
            f"SELECT url FROM failed_urls WHERE url IN ({placeholders})", urls
        ).fetchall()
    return {r["url"] for r in rows}


# ─── URL 访问记录 ─────────────────────────────────────────────────────────────

_URL_VISIT_TTL_DAYS = int(os.getenv("JOB_TTL_DAYS", 7))


def get_url_visit(url: str) -> dict | None:
    """
    查询 URL 访问记录。TTL 与 job_cache 相同（默认 7 天）。
    过期或未命中返回 None，让调用方重新抓取。
    """
    with _conn() as con:
        row = con.execute(
            "SELECT title, status, visited_at FROM url_visits WHERE url = ?", (url,)
        ).fetchone()
    if row is None:
        return None
    age = (datetime.utcnow() - datetime.fromisoformat(row["visited_at"])).days
    if age > _URL_VISIT_TTL_DAYS:
        return None
    return {"title": row["title"], "status": row["status"], "visited_at": row["visited_at"]}


def record_url_visit(url: str, title: str, status: str) -> None:
    """
    记录或更新一条 URL 访问记录。
    status 取值：fetched / empty / error / verification_failed
    """
    with _conn() as con:
        con.execute(
            """
            INSERT INTO url_visits (url, title, status, visited_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title      = excluded.title,
                status     = excluded.status,
                visited_at = excluded.visited_at
            """,
            (url, title, status, datetime.utcnow().isoformat()),
        )


# ─── CV 缓存 ──────────────────────────────────────────────────────────────────


def get_cv_profile(cv_hash: str, prompt_version: str = "") -> CVProfile | None:
    """按 CV 文本哈希查找缓存的解析结果，未命中返回 None。"""
    with _conn() as con:
        row = con.execute(
            "SELECT profile_json, prompt_version FROM cv_cache WHERE cv_hash = ?", (cv_hash,)
        ).fetchone()
    if row is None:
        return None
    if prompt_version and row["prompt_version"] != prompt_version:
        return None
    return CVProfile.model_validate_json(row["profile_json"])


def save_cv_profile(cv_hash: str, profile: CVProfile, prompt_version: str = "") -> None:
    """将 CVProfile 解析结果写入缓存（已存在则覆盖）。"""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO cv_cache (cv_hash, profile_json, prompt_version, cached_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cv_hash) DO UPDATE SET profile_json = excluded.profile_json,
                                               prompt_version = excluded.prompt_version,
                                               cached_at = excluded.cached_at
            """,
            (cv_hash, profile.model_dump_json(), prompt_version, datetime.utcnow().isoformat()),
        )


def get_latest_cv_profile() -> CVProfile | None:
    """返回最近缓存的 CVProfile（assess 命令无 CV 路径时使用）。"""
    with _conn() as con:
        row = con.execute(
            "SELECT profile_json FROM cv_cache ORDER BY cached_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return CVProfile.model_validate_json(row["profile_json"])


def get_latest_cv_hash() -> str:
    with _conn() as con:
        row = con.execute(
            "SELECT cv_hash FROM cv_cache ORDER BY cached_at DESC LIMIT 1"
        ).fetchone()
    return row["cv_hash"] if row is not None else ""


def get_unassessed_jobs(limit: int = 200) -> list[JobResult]:
    """返回当前 CV 下尚无匹配结果的未过期职位。

    只按现代 ``job_matches`` 判定：legacy ``assessment`` 列没有 cv_hash 归属，
    用它筛选会把换 CV 后本该重评的职位挡在门外。
    """
    with _conn() as con:
        rows = con.execute("SELECT * FROM job_cache ORDER BY fetched_at DESC").fetchall()

    unassessed: list[JobResult] = []
    for row in rows:
        job = _row_to_job(row)
        if job.is_expired:
            continue
        # 先过滤过期再挂载 match，避免为已过期职位做多余的查询。
        _attach_latest_match(job)
        if job.match_score is not None:
            continue
        unassessed.append(job)
        if len(unassessed) >= limit:
            break
    return unassessed


# ─── 流式搜索候选缓存 ─────────────────────────────────────────────────────────


def save_search_candidates(run_id: str, jobs: list[dict]) -> list[str]:
    """先持久化 filtered list，再由内存 worker 消费同一批 Python 对象。"""
    if not jobs:
        return []
    now = datetime.utcnow().isoformat()
    rows: list[tuple[str, str, str, str, str]] = []
    keys: list[str] = []
    for job in jobs:
        dedup_key = make_dedup_key(
            str(job.get("company") or ""),
            str(job.get("title") or ""),
        )
        keys.append(dedup_key)
        rows.append((run_id, dedup_key, json.dumps(job, ensure_ascii=False, default=str), now, now))
    with _conn() as con:
        con.executemany(
            """
            INSERT INTO search_candidates
              (run_id, dedup_key, candidate_json, status, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, ?)
            ON CONFLICT(run_id, dedup_key) DO UPDATE SET
              candidate_json = excluded.candidate_json,
              status = 'queued',
              updated_at = excluded.updated_at
            """,
            rows,
        )
    return keys


def update_search_candidate_status(run_id: str, dedup_keys: list[str], status: str) -> None:
    if not dedup_keys:
        return
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.executemany(
            """
            UPDATE search_candidates
            SET status = ?, updated_at = ?
            WHERE run_id = ? AND dedup_key = ?
            """,
            [(status, now, run_id, key) for key in dedup_keys],
        )


def get_search_candidates(run_id: str) -> list[dict]:
    """测试和故障检查用；正常 worker 不从数据库回读候选列表。"""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT dedup_key, candidate_json, status
            FROM search_candidates
            WHERE run_id = ?
            ORDER BY created_at, dedup_key
            """,
            (run_id,),
        ).fetchall()
    return [
        {
            "dedup_key": row["dedup_key"],
            "candidate": json.loads(row["candidate_json"]),
            "status": row["status"],
        }
        for row in rows
    ]


def prune_search_candidates(max_age_days: int = 14) -> int:
    with _conn() as con:
        result = con.execute(
            """
            DELETE FROM search_candidates
            WHERE julianday('now') - julianday(updated_at) > ?
            """,
            (max_age_days,),
        )
    return result.rowcount or 0


# ─── 缓存管理命令 ──────────────────────────────────────────────────────────────


def clear_all() -> None:
    """清空所有缓存。"""
    with _conn() as con:
        con.execute("DELETE FROM job_cache")
        con.execute("DELETE FROM jd_profiles")
        con.execute("DELETE FROM job_summaries")
        con.execute("DELETE FROM job_matches")
        con.execute("DELETE FROM job_relevance_rejections")
        con.execute("DELETE FROM interview_preps")
        con.execute("DELETE FROM cover_letters")
        con.execute("DELETE FROM cv_optimizations")
        con.execute("DELETE FROM search_sessions")
        con.execute("DELETE FROM failed_urls")
        con.execute("DELETE FROM cv_cache")
        con.execute("DELETE FROM url_visits")
        con.execute("DELETE FROM search_stats")
        con.execute("DELETE FROM filter_events")
        con.execute("DELETE FROM search_candidates")


def delete_jobs(dedup_keys: list[str]) -> int:
    """按 dedup_key 删除指定职位，返回实际删除条数。"""
    if not dedup_keys:
        return 0
    placeholders = ",".join("?" * len(dedup_keys))
    with _conn() as con:
        con.execute(
            f"DELETE FROM jd_profiles WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        con.execute(
            f"DELETE FROM job_summaries WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        con.execute(
            f"DELETE FROM job_matches WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        con.execute(
            f"DELETE FROM job_relevance_rejections WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        con.execute(
            f"DELETE FROM interview_preps WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        con.execute(
            f"DELETE FROM cover_letters WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        con.execute(
            f"DELETE FROM cv_optimizations WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        r = con.execute(
            f"DELETE FROM job_cache WHERE dedup_key IN ({placeholders})",
            dedup_keys,
        )
    return r.rowcount or 0


def save_search_stats(
    location: str,
    roles: list[str],
    provider: str,
    model: str,
    elapsed: float,
    tokens_in: int,
    tokens_out: int,
    jobs_found: int,
    run_id: str = "",
    experiment_name: str = "",
    notes: str = "",
    scraped_total: int = 0,
    deduped_total: int = 0,
    filtered_total: int = 0,
    new_jobs: int = 0,
    funnel: dict | None = None,
    cv_hash: str = "",
    app_version: str = "",
    cv_prompt_version: str = "",
    jd_summary_prompt_version: str = "",
    match_prompt_version: str = "",
    title_relevance_prompt_version: str = "",
    title_gate_version: str = "",
    coarse_filter_version: str = "",
    module_metrics: dict | None = None,
) -> int:
    """记录一次搜索的耗时和 token 消耗，返回插入行的 id。"""
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO search_stats
               (created_at, run_id, experiment_name, notes, location, roles, provider, model, elapsed, tokens_in, tokens_out, jobs_found, scraped_total, deduped_total, filtered_total, new_jobs, funnel_json, cv_hash, app_version, cv_prompt_version, jd_summary_prompt_version, match_prompt_version, title_relevance_prompt_version, title_gate_version, coarse_filter_version, module_metrics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                run_id,
                experiment_name,
                notes,
                location,
                json.dumps(roles, ensure_ascii=False),
                provider,
                model,
                round(elapsed, 1),
                tokens_in,
                tokens_out,
                jobs_found,
                scraped_total,
                deduped_total,
                filtered_total,
                new_jobs,
                json.dumps(funnel, ensure_ascii=False) if funnel else None,
                cv_hash,
                app_version,
                cv_prompt_version,
                jd_summary_prompt_version,
                match_prompt_version,
                title_relevance_prompt_version,
                title_gate_version,
                coarse_filter_version,
                json.dumps(module_metrics, ensure_ascii=False) if module_metrics else None,
            ),
        )
        return cur.lastrowid or 0


def record_filter_event(
    *,
    stage: str,
    title: str,
    company: str = "",
    location: str = "",
    source: str = "",
    url: str = "",
    reason: str = "",
    details: dict | None = None,
    run_id: str = "",
) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO filter_events
               (created_at, run_id, stage, title, company, location, source, url, reason, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                run_id,
                stage,
                title,
                company,
                location,
                source,
                url,
                reason,
                json.dumps(details, ensure_ascii=False) if details else None,
            ),
        )


def get_filter_events(run_id: str = "", limit: int = 500) -> list[dict]:
    with _conn() as con:
        if run_id:
            rows = con.execute(
                "SELECT * FROM filter_events WHERE run_id = ? ORDER BY id ASC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM filter_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        raw_details = item.pop("details_json", None)
        item["details"] = json.loads(raw_details) if raw_details else {}
        result.append(item)
    return result


def _derive_history_metrics(row: sqlite3.Row, funnel: dict | None) -> dict[str, int]:
    keys = row.keys()
    row_scraped = row["scraped_total"] if "scraped_total" in keys else 0
    row_deduped = row["deduped_total"] if "deduped_total" in keys else 0
    row_filtered = row["filtered_total"] if "filtered_total" in keys else 0
    row_new = row["new_jobs"] if "new_jobs" in keys else 0
    scraped_total = int(row_scraped or (funnel or {}).get("scraped_total") or 0)
    deduped_total = int(
        row_deduped
        or max(
            0,
            int((funnel or {}).get("prefilter_in") or 0) - int((funnel or {}).get("skip_dup") or 0),
        )
    )
    filtered_total = int(row_filtered or row["jobs_found"] or (funnel or {}).get("saved") or 0)
    new_jobs = int(
        row_new
        or max(
            0,
            int((funnel or {}).get("new_saved") or 0)
            or (filtered_total - int((funnel or {}).get("cache_hit") or 0) - int((funnel or {}).get("cache_patch") or 0)),
        )
    )
    return {
        "scraped_total": scraped_total,
        "deduped_total": deduped_total,
        "filtered_total": filtered_total,
        "new_jobs": new_jobs,
    }


def _safe_div(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return round(numerator / denominator, 3)


def _version_info(row: sqlite3.Row) -> dict[str, str]:
    keys = row.keys()
    return {
        "app_version": row["app_version"] if "app_version" in keys else "",
        "cv_prompt_version": row["cv_prompt_version"] if "cv_prompt_version" in keys else "",
        "jd_summary_prompt_version": row["jd_summary_prompt_version"] if "jd_summary_prompt_version" in keys else "",
        "match_prompt_version": row["match_prompt_version"] if "match_prompt_version" in keys else "",
        "title_relevance_prompt_version": row["title_relevance_prompt_version"] if "title_relevance_prompt_version" in keys else "",
        "title_gate_version": row["title_gate_version"] if "title_gate_version" in keys else "",
        "coarse_filter_version": row["coarse_filter_version"] if "coarse_filter_version" in keys else "",
    }


def _benchmark_signature(version_info: dict[str, str]) -> str:
    parts = [
        version_info.get("app_version", ""),
        version_info.get("cv_prompt_version", ""),
        version_info.get("jd_summary_prompt_version", ""),
        version_info.get("match_prompt_version", ""),
        version_info.get("title_relevance_prompt_version", ""),
        version_info.get("title_gate_version", ""),
        version_info.get("coarse_filter_version", ""),
    ]
    return "|".join(part or "-" for part in parts)


def _derive_benchmark_metrics(record: dict) -> dict[str, float]:
    funnel = record.get("funnel") or {}
    scraped_total = float(record.get("scraped_total") or 0)
    filtered_total = float(record.get("filtered_total") or 0)
    new_jobs = float(record.get("new_jobs") or 0)
    tokens_total = float((record.get("tokens_in") or 0) + (record.get("tokens_out") or 0))
    prefilter_in = float(funnel.get("prefilter_in") or 0)
    skip_seniority = float(funnel.get("skip_seniority") or 0)
    llm_assessed = float(funnel.get("llm_assessed") or 0)
    return {
        "prefilter_pass_rate": _safe_div(filtered_total, scraped_total),
        "new_job_yield": _safe_div(new_jobs, scraped_total),
        "tokens_per_filtered_job": round(tokens_total / filtered_total, 1) if filtered_total else 0.0,
        "tokens_per_new_job": round(tokens_total / new_jobs, 1) if new_jobs else 0.0,
        "assessment_efficiency": _safe_div(llm_assessed, new_jobs),
        "seniority_rejection_rate": _safe_div(skip_seniority, prefilter_in),
    }


def get_search_stats(limit: int = 50) -> list[dict]:
    """返回最近 N 条搜索记录，最新在前。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM search_stats ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        keys = row.keys()
        raw_funnel = row["funnel_json"] if "funnel_json" in keys else None
        funnel = json.loads(raw_funnel) if raw_funnel else None
        raw_module_metrics = row["module_metrics_json"] if "module_metrics_json" in keys else None
        module_metrics = json.loads(raw_module_metrics) if raw_module_metrics else None
        derived = _derive_history_metrics(row, funnel)
        versions = _version_info(row)
        record = {
            "id":         row["id"],
            "created_at": row["created_at"],
            "run_id": row["run_id"] if "run_id" in keys else "",
            "experiment_name": row["experiment_name"] if "experiment_name" in keys else "",
            "notes": row["notes"] if "notes" in keys else "",
            "location":   row["location"],
            "roles":      json.loads(row["roles"]),
            "provider":   row["provider"],
            "model":      row["model"],
            "elapsed":    row["elapsed"],
            "tokens_in":  row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "jobs_found": row["jobs_found"],
            "scraped_total": derived["scraped_total"],
            "deduped_total": derived["deduped_total"],
            "filtered_total": derived["filtered_total"],
            "new_jobs": derived["new_jobs"],
            "funnel":     funnel,
            "module_metrics": module_metrics,
            "versions": versions,
            "benchmark_signature": _benchmark_signature(versions),
        }
        record["benchmark"] = _derive_benchmark_metrics(record)
        result.append(record)
    return result


def get_stats_summary() -> dict:
    """返回全量统计合计。"""
    with _conn() as con:
        row = con.execute(
            """SELECT COUNT(*) as total_searches,
                      SUM(tokens_in)  as total_tokens_in,
                      SUM(tokens_out) as total_tokens_out,
                      SUM(elapsed)    as total_elapsed,
                      SUM(jobs_found) as total_jobs
               FROM search_stats"""
        ).fetchone()
    return {
        "total_searches":    row["total_searches"] or 0,
        "total_tokens_in":   row["total_tokens_in"]  or 0,
        "total_tokens_out":  row["total_tokens_out"] or 0,
        "total_elapsed":     round(row["total_elapsed"] or 0, 1),
        "total_jobs":        row["total_jobs"] or 0,
    }


def get_benchmark_summary(limit: int = 50) -> dict:
    records = get_search_stats(limit=limit)
    if not records:
        return {
            "current": None,
            "previous": None,
            "delta": None,
        }

    latest_signature = records[0]["benchmark_signature"]
    current_group = [r for r in records if r["benchmark_signature"] == latest_signature]
    previous_group: list[dict] = []
    for record in records:
        if record["benchmark_signature"] != latest_signature:
            previous_signature = record["benchmark_signature"]
            previous_group = [r for r in records if r["benchmark_signature"] == previous_signature]
            break

    def _aggregate(group: list[dict]) -> dict | None:
        if not group:
            return None
        count = len(group)
        latest = group[0]
        keys = (
            "prefilter_pass_rate",
            "new_job_yield",
            "tokens_per_filtered_job",
            "tokens_per_new_job",
            "assessment_efficiency",
            "seniority_rejection_rate",
        )
        metrics = {
            key: round(sum(float((item.get("benchmark") or {}).get(key) or 0) for item in group) / count, 3)
            for key in keys
        }
        return {
            "count": count,
            "signature": latest["benchmark_signature"],
            "versions": latest["versions"],
            "metrics": metrics,
        }

    current = _aggregate(current_group)
    previous = _aggregate(previous_group)
    delta = None
    if current and previous:
        delta = {
            key: round(current["metrics"][key] - previous["metrics"].get(key, 0), 3)
            for key in current["metrics"]
        }
    return {
        "current": current,
        "previous": previous,
        "delta": delta,
    }


def clear_search_stats() -> None:
    """清空全部搜索历史记录。"""
    with _conn() as con:
        con.execute("DELETE FROM search_stats")
        con.execute("DELETE FROM filter_events")


def _stale_match_conditions(
    prompt_version: str, keep_cv_hash: str, drop_orphans: bool
) -> tuple[list[str], list[str]]:
    """构造 job_matches 的过时行判定条件，返回 (SQL 片段, 参数)。"""
    clauses: list[str] = []
    params: list[str] = []
    if prompt_version:
        clauses.append("prompt_version != ?")
        params.append(prompt_version)
    if keep_cv_hash:
        clauses.append("cv_hash != ?")
        params.append(keep_cv_hash)
    if drop_orphans:
        clauses.append("job_id NOT IN (SELECT dedup_key FROM job_cache)")
    return clauses, params


def prune_job_matches(
    *,
    prompt_version: str = "",
    keep_cv_hash: str = "",
    drop_orphans: bool = False,
    dry_run: bool = True,
) -> dict[str, int]:
    """删除过时的匹配结果，被删除的职位会在下次搜索或 assess 时按当前口径重算。

    三类条件独立启用，彼此可重叠（同一行可能既是旧 prompt 版本又属于旧 CV），
    因此各分类计数之和可能大于 ``total``；``total`` 才是唯一行数。
    ``dry_run=True`` 时只统计不删除。
    """
    clauses, params = _stale_match_conditions(prompt_version, keep_cv_hash, drop_orphans)
    result = {"stale_version": 0, "stale_cv": 0, "orphan": 0, "total": 0, "deleted": 0}
    if not clauses:
        return result

    with _conn() as con:
        if prompt_version:
            result["stale_version"] = con.execute(
                "SELECT count(*) FROM job_matches WHERE prompt_version != ?", (prompt_version,)
            ).fetchone()[0]
        if keep_cv_hash:
            result["stale_cv"] = con.execute(
                "SELECT count(*) FROM job_matches WHERE cv_hash != ?", (keep_cv_hash,)
            ).fetchone()[0]
        if drop_orphans:
            result["orphan"] = con.execute(
                "SELECT count(*) FROM job_matches WHERE job_id NOT IN (SELECT dedup_key FROM job_cache)"
            ).fetchone()[0]

        where = " OR ".join(clauses)
        result["total"] = con.execute(
            f"SELECT count(*) FROM job_matches WHERE {where}", params
        ).fetchone()[0]
        if not dry_run:
            result["deleted"] = con.execute(
                f"DELETE FROM job_matches WHERE {where}", params
            ).rowcount
    return result


def clean_expired() -> int:
    """删除过期 JD 和 Session，返回删除条数。"""
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        expired_rows = con.execute(
            """
            SELECT dedup_key FROM job_cache
            WHERE (expires_at IS NOT NULL AND expires_at < ?)
               OR (expires_at IS NULL AND julianday('now') - julianday(fetched_at) > ?)
            """,
            (now, int(os.getenv("JOB_TTL_DAYS", 7))),
        ).fetchall()
        expired_job_ids = [row["dedup_key"] for row in expired_rows]
        if expired_job_ids:
            placeholders = ",".join("?" * len(expired_job_ids))
            con.execute(
                f"DELETE FROM jd_profiles WHERE job_id IN ({placeholders})",
                expired_job_ids,
            )
            con.execute(
                f"DELETE FROM job_summaries WHERE job_id IN ({placeholders})",
                expired_job_ids,
            )
            con.execute(
                f"DELETE FROM job_relevance_rejections WHERE job_id IN ({placeholders})",
                expired_job_ids,
            )
        # 删除有明确截止日期且已过期的 JD
        r1 = con.execute(
            "DELETE FROM job_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        # 删除超过 TTL 天数的 JD（无截止日期）
        r2 = con.execute(
            "DELETE FROM job_cache WHERE expires_at IS NULL AND julianday('now') - julianday(fetched_at) > ?",
            (int(os.getenv("JOB_TTL_DAYS", 7)),),
        )
        # 删除过期 Session
        r3 = con.execute(
            "DELETE FROM search_sessions WHERE (julianday('now') - julianday(created_at)) * 24 > ?",
            (int(os.getenv("SESSION_TTL_HOURS", 24)),),
        )
        # 删除过期 URL 访问记录
        r4 = con.execute(
            "DELETE FROM url_visits WHERE julianday('now') - julianday(visited_at) > ?",
            (int(os.getenv("JOB_TTL_DAYS", 7)),),
        )
    return (r1.rowcount or 0) + (r2.rowcount or 0) + (r3.rowcount or 0) + (r4.rowcount or 0)
