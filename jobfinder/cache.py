"""SQLite 缓存层：JobResult / SearchSession / FailedURL 三张表。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from jobfinder.schemas import CVProfile, CompanyProfile, FailedURL, JobAssessment, JobResult, SearchSession

_DEFAULT_DB_PATH = "jobfinder_cache.db"

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS job_cache (
    dedup_key           TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    location            TEXT,
    description_snippet TEXT,
    url                 TEXT,
    sources             TEXT,       -- JSON array
    fetched_at          TEXT NOT NULL,
    expires_at          TEXT,       -- NULL 表示无截止日期
    is_complete         INTEGER NOT NULL DEFAULT 1,
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

CREATE TABLE IF NOT EXISTS company_cache (
    company_key  TEXT PRIMARY KEY,  -- normalize_company(name)
    profile_json TEXT NOT NULL,     -- CompanyProfile JSON
    cached_at    TEXT NOT NULL
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
    jobs_found  INTEGER NOT NULL DEFAULT 0
);
"""

# 迁移语句：对已存在的旧表补加列（IF NOT EXISTS 语法 SQLite ≥ 3.37 支持）
_MIGRATE_SQL = """
ALTER TABLE job_cache ADD COLUMN assessment TEXT;
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
            "ALTER TABLE job_cache ADD COLUMN company_profile TEXT",
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


def get_job(dedup_key: str) -> JobResult | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM job_cache WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_job(row)


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
               url, sources, fetched_at, expires_at, is_complete, assessment, company_profile)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.dedup_key,
                job.title,
                job.company,
                job.location,
                job.description_snippet,
                job.url,
                json.dumps(job.sources),
                job.fetched_at.isoformat(),
                job.expires_at.isoformat() if job.expires_at else None,
                int(job.is_complete),
                job.assessment.model_dump_json() if job.assessment else None,
                job.company_profile.model_dump_json() if job.company_profile else None,
            ),
        )


def _merge_job(existing: JobResult, new: JobResult) -> None:
    """追加新来源；若新记录有 expires_at / company_profile / assessment，则更新。"""
    merged_sources = list(dict.fromkeys(existing.sources + new.sources))
    new_expires = new.expires_at or existing.expires_at
    new_company = new.company_profile or existing.company_profile
    new_assessment = new.assessment or existing.assessment

    with _conn() as con:
        con.execute(
            """
            UPDATE job_cache
            SET sources = ?, expires_at = ?, company_profile = ?, assessment = ?
            WHERE dedup_key = ?
            """,
            (
                json.dumps(merged_sources),
                new_expires.isoformat() if new_expires else None,
                new_company.model_dump_json() if new_company else None,
                new_assessment.model_dump_json() if new_assessment else None,
                existing.dedup_key,
            ),
        )


def get_recent_jobs(limit: int = 50) -> list[JobResult]:
    """按抓取时间倒序返回最近 limit 条未过期职位。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM job_cache ORDER BY fetched_at DESC LIMIT ?", (limit,)
        ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    return [j for j in jobs if not j.is_expired]


def get_job_by_url(url: str) -> JobResult | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM job_cache WHERE url = ?", (url,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_job(row)


def get_jobs_by_keys(dedup_keys: list[str]) -> list[JobResult]:
    if not dedup_keys:
        return []
    placeholders = ",".join("?" * len(dedup_keys))
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM job_cache WHERE dedup_key IN ({placeholders})",
            dedup_keys,
        ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    return [j for j in jobs if not j.is_expired]


def _row_to_job(row: sqlite3.Row) -> JobResult:
    keys = row.keys()
    raw_assessment = row["assessment"] if "assessment" in keys else None
    raw_company = row["company_profile"] if "company_profile" in keys else None
    assessment = JobAssessment.model_validate_json(raw_assessment) if raw_assessment else None
    company_profile = CompanyProfile.model_validate_json(raw_company) if raw_company else None
    return JobResult(
        title=row["title"],
        company=row["company"],
        location=row["location"] or "",
        url=row["url"],
        description_snippet=row["description_snippet"] or "",
        sources=json.loads(row["sources"] or "[]"),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        is_complete=bool(row["is_complete"]),
        assessment=assessment,
        company_profile=company_profile,
    )


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


def get_cv_profile(cv_hash: str) -> CVProfile | None:
    """按 CV 文本哈希查找缓存的解析结果，未命中返回 None。"""
    with _conn() as con:
        row = con.execute(
            "SELECT profile_json FROM cv_cache WHERE cv_hash = ?", (cv_hash,)
        ).fetchone()
    if row is None:
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


def save_cv_profile(cv_hash: str, profile: CVProfile) -> None:
    """将 CVProfile 解析结果写入缓存（已存在则覆盖）。"""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO cv_cache (cv_hash, profile_json, cached_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cv_hash) DO UPDATE SET profile_json = excluded.profile_json,
                                               cached_at = excluded.cached_at
            """,
            (cv_hash, profile.model_dump_json(), datetime.utcnow().isoformat()),
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


# ─── 公司信息缓存 ─────────────────────────────────────────────────────────────

_COMPANY_CACHE_TTL_DAYS = 30


def get_company_profile(company_key: str) -> CompanyProfile | None:
    """查询公司信息缓存（30天TTL），未命中或过期返回 None。"""
    with _conn() as con:
        row = con.execute(
            "SELECT profile_json, cached_at FROM company_cache WHERE company_key = ?",
            (company_key,),
        ).fetchone()
    if row is None:
        return None
    age = (datetime.utcnow() - datetime.fromisoformat(row["cached_at"])).days
    if age > _COMPANY_CACHE_TTL_DAYS:
        return None
    return CompanyProfile.model_validate_json(row["profile_json"])


def save_company_profile(company_key: str, profile: CompanyProfile) -> None:
    """将 CompanyProfile 写入缓存（已存在则覆盖）。"""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO company_cache (company_key, profile_json, cached_at)
            VALUES (?, ?, ?)
            ON CONFLICT(company_key) DO UPDATE SET profile_json = excluded.profile_json,
                                                   cached_at = excluded.cached_at
            """,
            (company_key, profile.model_dump_json(), datetime.utcnow().isoformat()),
        )


def update_job_company_profile(dedup_key: str, profile: CompanyProfile) -> None:
    """单独更新某条 JD 的 company_profile（公司信息异步补充时使用）。"""
    with _conn() as con:
        con.execute(
            "UPDATE job_cache SET company_profile = ? WHERE dedup_key = ?",
            (profile.model_dump_json(), dedup_key),
        )


def update_job_assessment(dedup_key: str, assessment: JobAssessment) -> None:
    """单独更新某条 JD 的 assessment（独立评估命令使用）。"""
    with _conn() as con:
        con.execute(
            "UPDATE job_cache SET assessment = ? WHERE dedup_key = ?",
            (assessment.model_dump_json(), dedup_key),
        )


def get_unassessed_jobs(limit: int = 200) -> list[JobResult]:
    """返回缓存中 assessment 为 NULL 且未过期的职位。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM job_cache WHERE assessment IS NULL ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    return [j for j in jobs if not j.is_expired]


# ─── 缓存管理命令 ──────────────────────────────────────────────────────────────


def clear_all() -> None:
    """清空所有缓存。"""
    with _conn() as con:
        con.execute("DELETE FROM job_cache")
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
) -> None:
    """记录一次搜索的耗时和 token 消耗。"""
    with _conn() as con:
        con.execute(
            """INSERT INTO search_stats
               (created_at, location, roles, provider, model, elapsed, tokens_in, tokens_out, jobs_found)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )


def get_search_stats(limit: int = 50) -> list[dict]:
    """返回最近 N 条搜索记录，最新在前。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM search_stats ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
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
