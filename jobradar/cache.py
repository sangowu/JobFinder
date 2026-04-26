"""SQLite 缓存层：JobResult / SearchSession / FailedURL 三张表。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
import hashlib
from pathlib import Path

from jobradar.schemas import CVOptimization, CoarseFilterResult, CoverLetter, CVProfile, FailedURL, InterviewPrep, JobAssessment, JobResult, JobSummary, MatchScore, SearchSession

_DEFAULT_DB_PATH = "jobradar_cache.db"

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

CREATE TABLE IF NOT EXISTS title_cache (
    cache_key   TEXT PRIMARY KEY,  -- cv_hash + "::" + countries
    result_json TEXT NOT NULL,     -- JSON: {titles: [...], keywords_used: [...]}
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
    cv_hash     TEXT NOT NULL DEFAULT ''
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


@contextmanager
def _conn():
    db_path = Path(os.getenv("CACHE_DB_PATH", _DEFAULT_DB_PATH))
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        con.executescript(_INIT_SQL)
        # 迁移：旧库补加 assessment 列（列已存在时 SQLite 会报错，忽略即可）
        for migration in (
            "ALTER TABLE job_cache ADD COLUMN assessment TEXT",
            "ALTER TABLE job_cache ADD COLUMN coarse_filter TEXT",
            "ALTER TABLE job_cache ADD COLUMN company_profile TEXT",  # deprecated, kept for old DB compat
            "ALTER TABLE job_cache ADD COLUMN date_posted TEXT DEFAULT ''",
            "ALTER TABLE job_cache ADD COLUMN raw_sources TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE search_stats ADD COLUMN funnel_json TEXT",
            "ALTER TABLE search_stats ADD COLUMN cv_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE search_stats ADD COLUMN scraped_total INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE search_stats ADD COLUMN deduped_total INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE search_stats ADD COLUMN filtered_total INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE search_stats ADD COLUMN new_jobs INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cv_cache ADD COLUMN prompt_version TEXT NOT NULL DEFAULT ''",
        ):
            try:
                con.execute(migration)
                con.commit()
            except Exception:
                pass
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ─── JobResult ────────────────────────────────────────────────────────────────


def get_job(dedup_key: str, language: str = "zh") -> JobResult | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM job_cache WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
    if row is None:
        return None
    job = _row_to_job(row)
    from jobradar.jd_summary import summary_prompt_version

    job.job_summary = get_job_summary(job.dedup_key, job.description_snippet, prompt_version=summary_prompt_version(language))
    if job.job_summary is None:
        job.job_summary = get_job_summary(job.dedup_key, job.description_snippet)
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
                json.dumps(job.sources),
                json.dumps(job.raw_sources),
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
    merged_sources = list(dict.fromkeys(existing.sources + new.sources))
    # raw_sources 按 source 去重合并
    existing_src_names = {r["source"] for r in existing.raw_sources}
    merged_raw = list(existing.raw_sources) + [r for r in new.raw_sources if r["source"] not in existing_src_names]
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
        existing = json.loads(row["sources"] or "[]")
        if source not in existing:
            existing.append(source)
            con.execute(
                "UPDATE job_cache SET sources = ? WHERE dedup_key = ?",
                (json.dumps(existing), dedup_key),
            )


def get_recent_jobs(limit: int = 50, language: str = "zh") -> list[JobResult]:
    """按抓取时间倒序返回最近 limit 条未过期职位。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM job_cache ORDER BY fetched_at DESC LIMIT ?", (limit,)
        ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    from jobradar.jd_summary import summary_prompt_version

    for job in jobs:
        job.job_summary = get_job_summary(job.dedup_key, job.description_snippet, prompt_version=summary_prompt_version(language))
        if job.job_summary is None:
            job.job_summary = get_job_summary(job.dedup_key, job.description_snippet)
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
    from jobradar.jd_summary import summary_prompt_version

    job.job_summary = get_job_summary(job.dedup_key, job.description_snippet, prompt_version=summary_prompt_version(language))
    if job.job_summary is None:
        job.job_summary = get_job_summary(job.dedup_key, job.description_snippet)
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
    from jobradar.jd_summary import summary_prompt_version

    for job in jobs:
        job.job_summary = get_job_summary(job.dedup_key, job.description_snippet, prompt_version=summary_prompt_version(language))
        if job.job_summary is None:
            job.job_summary = get_job_summary(job.dedup_key, job.description_snippet)
        _attach_latest_match(job, language=language)
    return [j for j in jobs if not j.is_expired]


def _row_to_job(row: sqlite3.Row) -> JobResult:
    keys = row.keys()
    raw_coarse_filter = row["coarse_filter"] if "coarse_filter" in keys else None
    raw_assessment = row["assessment"] if "assessment" in keys else None
    coarse_filter = CoarseFilterResult.model_validate_json(raw_coarse_filter) if raw_coarse_filter else None
    assessment = JobAssessment.model_validate_json(raw_assessment) if raw_assessment else None
    return JobResult(
        title=row["title"],
        company=row["company"],
        location=row["location"] or "",
        url=row["url"],
        description_snippet=row["description_snippet"] or "",
        sources=[s if isinstance(s, str) else s.get("source", "") for s in json.loads(row["sources"] or "[]")],
        raw_sources=json.loads(row["raw_sources"] if "raw_sources" in row.keys() and row["raw_sources"] else "[]"),
        date_posted=row["date_posted"] if "date_posted" in row.keys() and row["date_posted"] else "",
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        is_complete=bool(row["is_complete"]),
        coarse_filter=coarse_filter,
        assessment=assessment,
    )


def _description_hash(description: str) -> str:
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


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
    if profile is not None and job.job_summary is not None:
        from jobradar.matching import adjust_match_for_profile

        match = adjust_match_for_profile(profile, job.job_summary, match, language=language)
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


def get_interview_prep(job_id: str, cv_hash: str, description: str = "") -> InterviewPrep | None:
    with _conn() as con:
        row = con.execute(
            "SELECT prep_json, description_hash FROM interview_preps WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != _description_hash(description):
        return None
    return InterviewPrep.model_validate_json(row["prep_json"])


def save_interview_prep(
    prep: InterviewPrep,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO interview_preps
              (job_id, cv_hash, description_hash, prep_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              prep_json = excluded.prep_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                prep.job_id,
                prep.cv_hash,
                _description_hash(description),
                prep.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_cover_letter(job_id: str, cv_hash: str, description: str = "") -> CoverLetter | None:
    with _conn() as con:
        row = con.execute(
            "SELECT letter_json, description_hash FROM cover_letters WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != _description_hash(description):
        return None
    return CoverLetter.model_validate_json(row["letter_json"])


def save_cover_letter(
    letter: CoverLetter,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO cover_letters
              (job_id, cv_hash, description_hash, letter_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              letter_json = excluded.letter_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                letter.job_id,
                letter.cv_hash,
                _description_hash(description),
                letter.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_cv_optimization(job_id: str, cv_hash: str, description: str = "") -> CVOptimization | None:
    with _conn() as con:
        row = con.execute(
            "SELECT optimization_json, description_hash FROM cv_optimizations WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != _description_hash(description):
        return None
    return CVOptimization.model_validate_json(row["optimization_json"])


def save_cv_optimization(
    optimization: CVOptimization,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO cv_optimizations
              (job_id, cv_hash, description_hash, optimization_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              optimization_json = excluded.optimization_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                optimization.job_id,
                optimization.cv_hash,
                _description_hash(description),
                optimization.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_job_artifacts(job_id: str, cv_hash: str, description: str = "") -> dict[str, dict]:
    description_hash = _description_hash(description) if description else ""
    with _conn() as con:
        prep_row = con.execute(
            "SELECT prep_json, description_hash, updated_at FROM interview_preps WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
        letter_row = con.execute(
            "SELECT letter_json, description_hash, updated_at FROM cover_letters WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
        optimization_row = con.execute(
            "SELECT optimization_json, description_hash, updated_at FROM cv_optimizations WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()

    def _entry(row, field: str, model_cls):
        if row is None:
            return {"exists": False, "stale": False, "updated_at": None, "data": None}
        stale = bool(description_hash) and row["description_hash"] != description_hash
        return {
            "exists": not stale,
            "stale": stale,
            "updated_at": row["updated_at"],
            "data": None if stale else model_cls.model_validate_json(row[field]).model_dump(mode="json"),
        }

    return {
        "interview_prep": _entry(prep_row, "prep_json", InterviewPrep),
        "cover_letter": _entry(letter_row, "letter_json", CoverLetter),
        "cv_optimization": _entry(optimization_row, "optimization_json", CVOptimization),
    }


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


_TITLE_CACHE_TTL_DAYS = 7


def get_title_cache(cache_key: str) -> dict | None:
    """返回缓存的 title 发现结果（{titles, keywords_used}），过期或未命中返回 None。"""
    with _conn() as con:
        row = con.execute(
            "SELECT result_json, cached_at FROM title_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if row is None:
        return None
    age = (datetime.utcnow() - datetime.fromisoformat(row["cached_at"])).days
    if age > _TITLE_CACHE_TTL_DAYS:
        return None
    return json.loads(row["result_json"])


def save_title_cache(cache_key: str, result_json: str) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT INTO title_cache (cache_key, result_json, cached_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET result_json = excluded.result_json,
                                                 cached_at = excluded.cached_at
            """,
            (cache_key, result_json, datetime.utcnow().isoformat()),
        )


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


def update_job_assessment(dedup_key: str, assessment: JobAssessment) -> None:
    """单独更新某条 JD 的 assessment（独立评估命令使用）。"""
    with _conn() as con:
        con.execute(
            "UPDATE job_cache SET assessment = ? WHERE dedup_key = ?",
            (assessment.model_dump_json(), dedup_key),
        )


def get_unassessed_jobs(limit: int = 200) -> list[JobResult]:
    """返回尚无现代匹配结果且缺少 legacy assessment 的未过期职位。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM job_cache WHERE assessment IS NULL ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    for job in jobs:
        _attach_latest_match(job)
    return [j for j in jobs if not j.is_expired and job.match_score is None]


# ─── 缓存管理命令 ──────────────────────────────────────────────────────────────


def clear_all() -> None:
    """清空所有缓存。"""
    with _conn() as con:
        con.execute("DELETE FROM job_cache")
        con.execute("DELETE FROM job_summaries")
        con.execute("DELETE FROM job_matches")
        con.execute("DELETE FROM interview_preps")
        con.execute("DELETE FROM cover_letters")
        con.execute("DELETE FROM cv_optimizations")
        con.execute("DELETE FROM search_sessions")
        con.execute("DELETE FROM failed_urls")
        con.execute("DELETE FROM cv_cache")
        con.execute("DELETE FROM title_cache")
        con.execute("DELETE FROM url_visits")


def delete_jobs(dedup_keys: list[str]) -> int:
    """按 dedup_key 删除指定职位，返回实际删除条数。"""
    if not dedup_keys:
        return 0
    placeholders = ",".join("?" * len(dedup_keys))
    with _conn() as con:
        con.execute(
            f"DELETE FROM job_summaries WHERE job_id IN ({placeholders})",
            dedup_keys,
        )
        con.execute(
            f"DELETE FROM job_matches WHERE job_id IN ({placeholders})",
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
    scraped_total: int = 0,
    deduped_total: int = 0,
    filtered_total: int = 0,
    new_jobs: int = 0,
    funnel: dict | None = None,
    cv_hash: str = "",
) -> int:
    """记录一次搜索的耗时和 token 消耗，返回插入行的 id。"""
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO search_stats
               (created_at, location, roles, provider, model, elapsed, tokens_in, tokens_out, jobs_found, scraped_total, deduped_total, filtered_total, new_jobs, funnel_json, cv_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
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
            ),
        )
        return cur.lastrowid or 0


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
        derived = _derive_history_metrics(row, funnel)
        result.append({
            "id":         row["id"],
            "created_at": row["created_at"],
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
        })
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


def clear_search_stats() -> None:
    """清空全部搜索历史记录。"""
    with _conn() as con:
        con.execute("DELETE FROM search_stats")


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
                f"DELETE FROM job_summaries WHERE job_id IN ({placeholders})",
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
