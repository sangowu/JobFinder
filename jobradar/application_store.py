"""SQLite persistence for email-derived job application status."""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from jobradar.paths import DATA_DIR, ensure_parent
from jobradar.schemas import ApplicationEmailAnalysis, ApplicationEvent, JobApplication

_DEFAULT_DB_PATH = str(DATA_DIR / "jobradar_cache.db")
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS processed_emails (
    provider TEXT NOT NULL,
    provider_message_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body_hash TEXT NOT NULL DEFAULT '',
    analysis_json TEXT NOT NULL,
    gmail_thread_id TEXT NOT NULL DEFAULT '',
    gmail_history_id TEXT NOT NULL DEFAULT '',
    classification_reason TEXT NOT NULL DEFAULT '',
    classifier_version TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL,
    PRIMARY KEY (provider, provider_message_id)
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    company TEXT NOT NULL DEFAULT '',
    job_title TEXT NOT NULL DEFAULT '',
    company_key TEXT NOT NULL DEFAULT '',
    job_title_key TEXT NOT NULL DEFAULT '',
    current_status TEXT NOT NULL DEFAULT 'unknown',
    applied_at TEXT,
    last_event_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'email',
    external_reference TEXT,
    gmail_thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    email_message_id TEXT NOT NULL DEFAULT '',
    gmail_thread_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(id),
    UNIQUE(email_message_id, event_type)
);
CREATE TABLE IF NOT EXISTS email_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0,
    already_processed INTEGER NOT NULL DEFAULT 0,
    scanned INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    job_related INTEGER NOT NULL DEFAULT 0,
    subscription_filtered INTEGER NOT NULL DEFAULT 0,
    unrelated INTEGER NOT NULL DEFAULT 0,
    unknown INTEGER NOT NULL DEFAULT 0,
    failed_messages INTEGER NOT NULL DEFAULT 0,
    sync_mode TEXT NOT NULL DEFAULT 'full',
    history_id TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS email_sync_state (
    provider TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0,
    history_id TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

_MIGRATION_COLUMNS = {
    "processed_emails": {
        "gmail_thread_id": "TEXT NOT NULL DEFAULT ''",
        "gmail_history_id": "TEXT NOT NULL DEFAULT ''",
        "classification_reason": "TEXT NOT NULL DEFAULT ''",
        "classifier_version": "TEXT NOT NULL DEFAULT ''",
    },
    "applications": {
        "gmail_thread_id": "TEXT",
        "company_key": "TEXT NOT NULL DEFAULT ''",
        "job_title_key": "TEXT NOT NULL DEFAULT ''",
    },
    "application_events": {"gmail_thread_id": "TEXT NOT NULL DEFAULT ''"},
    "email_sync_runs": {
        "pages": "INTEGER NOT NULL DEFAULT 0",
        "job_related": "INTEGER NOT NULL DEFAULT 0",
        "subscription_filtered": "INTEGER NOT NULL DEFAULT 0",
        "unrelated": "INTEGER NOT NULL DEFAULT 0",
        "unknown": "INTEGER NOT NULL DEFAULT 0",
        "failed_messages": "INTEGER NOT NULL DEFAULT 0",
        "sync_mode": "TEXT NOT NULL DEFAULT 'full'",
        "history_id": "TEXT NOT NULL DEFAULT ''",
    },
}


@contextmanager
def _conn():
    path = Path(os.getenv("CACHE_DB_PATH", _DEFAULT_DB_PATH))
    ensure_parent(path)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=5000")
        con.executescript(_INIT_SQL)
        _migrate(con)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    con.executescript(
        "CREATE INDEX IF NOT EXISTS idx_application_events_application_time "
        "ON application_events(application_id, event_at DESC);"
        "CREATE INDEX IF NOT EXISTS idx_applications_reference ON applications(external_reference);"
        "CREATE INDEX IF NOT EXISTS idx_applications_thread ON applications(gmail_thread_id);"
        "CREATE INDEX IF NOT EXISTS idx_applications_identity "
        "ON applications(company_key, job_title_key);"
    )
    rows = con.execute(
        "SELECT id, company, job_title FROM applications "
        "WHERE company_key='' OR job_title_key=''"
    ).fetchall()
    for row in rows:
        con.execute(
            "UPDATE applications SET company_key=?, job_title_key=? WHERE id=?",
            (_identity_key(row["company"], company=True), _identity_key(row["job_title"]), row["id"]),
        )


def _identity_key(value: str, *, company: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()
    if company:
        normalized = re.sub(
            r"\s+(?:limited|ltd|incorporated|inc|llc|plc|gmbh|corp|corporation)$", "", normalized
        )
    return re.sub(r"\s+", " ", normalized)


def email_was_processed(provider: str, message_id: str) -> bool:
    with _conn() as con:
        return con.execute(
            "SELECT 1 FROM processed_emails WHERE provider=? AND provider_message_id=?",
            (provider, message_id),
        ).fetchone() is not None


def record_email(
    *, provider: str, message_id: str, received_at: datetime, sender: str,
    subject: str, body_hash: str, analysis: ApplicationEmailAnalysis,
    thread_id: str = "", history_id: str = "",
) -> JobApplication | None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        if con.execute(
            "SELECT 1 FROM processed_emails WHERE provider=? AND provider_message_id=?",
            (provider, message_id),
        ).fetchone():
            return None
        con.execute(
            "INSERT INTO processed_emails (provider, provider_message_id, received_at, sender, "
            "subject, body_hash, analysis_json, gmail_thread_id, gmail_history_id, "
            "classification_reason, classifier_version, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (provider, message_id, received_at.isoformat(), sender, subject, body_hash,
             analysis.model_dump_json(), thread_id, history_id, analysis.classification_reason,
             analysis.classifier_version, now),
        )
        if not analysis.is_job_related:
            return None
        event_at = analysis.event_at or received_at
        company = analysis.company.strip() or "Unknown company"
        title = analysis.job_title.strip() or "Unknown role"
        company_key = _identity_key(company, company=True)
        title_key = _identity_key(title)
        row = None
        reference = (analysis.application_reference or "").strip()
        if reference:
            row = con.execute(
                "SELECT id, last_event_at FROM applications "
                "WHERE lower(external_reference)=lower(?) ORDER BY updated_at DESC LIMIT 1",
                (reference,),
            ).fetchone()
        if not row and thread_id:
            row = con.execute(
                "SELECT id, last_event_at FROM applications WHERE gmail_thread_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if not row and company != "Unknown company" and title != "Unknown role":
            row = con.execute(
                "SELECT id, last_event_at FROM applications "
                "WHERE company_key=? AND job_title_key=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (company_key, title_key),
            ).fetchone()
        if row:
            application_id = int(row["id"])
            if event_at.isoformat() >= row["last_event_at"]:
                con.execute(
                    "UPDATE applications SET current_status=?, last_event_at=?, updated_at=?, "
                    "external_reference=COALESCE(NULLIF(external_reference, ''), ?), "
                    "gmail_thread_id=COALESCE(NULLIF(gmail_thread_id, ''), ?) WHERE id=?",
                    (analysis.status, event_at.isoformat(), now, reference or None,
                     thread_id or None, application_id),
                )
            else:
                con.execute(
                    "UPDATE applications SET "
                    "external_reference=COALESCE(NULLIF(external_reference, ''), ?), "
                    "gmail_thread_id=COALESCE(NULLIF(gmail_thread_id, ''), ?), "
                    "updated_at=? WHERE id=?",
                    (reference or None, thread_id or None, now, application_id),
                )
        else:
            applied_at = event_at.isoformat() if analysis.status == "submitted" else None
            cursor = con.execute(
                "INSERT INTO applications (company, job_title, company_key, job_title_key, "
                "current_status, applied_at, last_event_at, "
                "external_reference, gmail_thread_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (company, title, company_key, title_key, analysis.status, applied_at, event_at.isoformat(),
                 reference or None, thread_id or None, now, now),
            )
            application_id = int(cursor.lastrowid)
        con.execute(
            "INSERT OR IGNORE INTO application_events (application_id, email_message_id, "
            "gmail_thread_id, event_type, event_at, confidence, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (application_id, message_id, thread_id, analysis.status, event_at.isoformat(),
             analysis.confidence, analysis.summary, now),
        )
    return get_application(application_id)


def list_applications(status: str | None = None) -> list[JobApplication]:
    with _conn() as con:
        sql = "SELECT * FROM applications"
        params: tuple[str, ...] = ()
        if status:
            sql += " WHERE current_status=?"
            params = (status,)
        rows = con.execute(sql + " ORDER BY last_event_at DESC", params).fetchall()
    return [_from_row(row) for row in rows]


def get_application(application_id: int) -> JobApplication | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if not row:
            return None
        events = con.execute(
            "SELECT * FROM application_events WHERE application_id=? ORDER BY event_at DESC",
            (application_id,),
        ).fetchall()
    application = _from_row(row)
    application.events = [ApplicationEvent(**dict(event)) for event in events]
    return application


def update_application(application_id: int, *, status: str, company: str, job_title: str) -> JobApplication | None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        existing = con.execute(
            "SELECT current_status FROM applications WHERE id=?", (application_id,)
        ).fetchone()
        if not existing:
            return None
        con.execute(
            "UPDATE applications SET current_status=?, company=?, job_title=?, company_key=?, "
            "job_title_key=?, updated_at=?, last_event_at=? WHERE id=?",
            (status, company.strip(), job_title.strip(), _identity_key(company, company=True),
             _identity_key(job_title), now, now, application_id),
        )
        if existing["current_status"] != status:
            con.execute(
                "INSERT INTO application_events (application_id, email_message_id, event_type, "
                "event_at, confidence, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (application_id, f"manual:{application_id}:{now}", status, now, 1.0,
                 "Confirmed by user", now),
            )
    return get_application(application_id)

def record_sync_run(
    *, trigger: str, status: str, started_at: datetime, completed_at: datetime,
    duration_ms: int, candidates: int = 0, already_processed: int = 0,
    scanned: int = 0, matched: int = 0, pages: int = 0, job_related: int = 0,
    subscription_filtered: int = 0, unrelated: int = 0, unknown: int = 0,
    failed_messages: int = 0, sync_mode: str = "full", history_id: str = "",
    error_message: str = "",
) -> dict:
    with _conn() as con:
        cursor = con.execute(
            "INSERT INTO email_sync_runs (trigger, status, started_at, completed_at, duration_ms, "
            "candidates, already_processed, scanned, matched, pages, job_related, "
            "subscription_filtered, unrelated, unknown, failed_messages, sync_mode, history_id, "
            "error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trigger, status, started_at.isoformat(), completed_at.isoformat(), duration_ms,
             candidates, already_processed, scanned, matched, pages, job_related,
             subscription_filtered, unrelated, unknown, failed_messages, sync_mode, history_id,
             error_message[:500]),
        )
        run_id = int(cursor.lastrowid)
    return get_sync_run(run_id)


def get_sync_run(run_id: int) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM email_sync_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else {}


def list_sync_runs(limit: int = 20) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM email_sync_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)
        ).fetchall()
    return [dict(row) for row in rows]


def latest_sync_run() -> dict | None:
    runs = list_sync_runs(1)
    return runs[0] if runs else None


def get_sync_state(provider: str = "gmail") -> dict:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO email_sync_state (provider, updated_at) VALUES (?, ?)",
            (provider, now),
        )
        row = con.execute(
            "SELECT * FROM email_sync_state WHERE provider=?", (provider,)
        ).fetchone()
    return dict(row)


def set_sync_paused(paused: bool, provider: str = "gmail") -> dict:
    get_sync_state(provider)
    with _conn() as con:
        con.execute(
            "UPDATE email_sync_state SET paused=?, updated_at=? WHERE provider=?",
            (int(paused), datetime.utcnow().isoformat(), provider),
        )
    return get_sync_state(provider)


def set_history_id(history_id: str, provider: str = "gmail") -> None:
    get_sync_state(provider)
    with _conn() as con:
        con.execute(
            "UPDATE email_sync_state SET history_id=?, updated_at=? WHERE provider=?",
            (history_id, datetime.utcnow().isoformat(), provider),
        )


def acquire_sync_lease(owner: str, provider: str = "gmail", ttl_seconds: int = 3600) -> bool:
    now = datetime.utcnow()
    get_sync_state(provider)
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT lease_owner, lease_until FROM email_sync_state WHERE provider=?", (provider,)
        ).fetchone()
        lease_active = bool(row["lease_until"] and row["lease_until"] > now.isoformat())
        if lease_active and row["lease_owner"] != owner:
            return False
        con.execute(
            "UPDATE email_sync_state SET lease_owner=?, lease_until=?, updated_at=? WHERE provider=?",
            (owner, (now + timedelta(seconds=ttl_seconds)).isoformat(), now.isoformat(), provider),
        )
    return True


def release_sync_lease(owner: str, provider: str = "gmail") -> None:
    with _conn() as con:
        con.execute(
            "UPDATE email_sync_state SET lease_owner='', lease_until='', updated_at=? "
            "WHERE provider=? AND lease_owner=?",
            (datetime.utcnow().isoformat(), provider, owner),
        )


def clear_email_data(*, reset_history: bool = True, pause: bool | None = None) -> None:
    with _conn() as con:
        con.execute("DELETE FROM application_events")
        con.execute("DELETE FROM applications")
        con.execute("DELETE FROM processed_emails")
        con.execute("DELETE FROM email_sync_runs")
        if reset_history:
            con.execute("UPDATE email_sync_state SET history_id='', updated_at=? WHERE provider='gmail'",
                        (datetime.utcnow().isoformat(),))
        if pause is not None:
            con.execute("UPDATE email_sync_state SET paused=?, updated_at=? WHERE provider='gmail'",
                        (int(pause), datetime.utcnow().isoformat()))

def delete_application(application_id: int) -> bool:
    with _conn() as con:
        row = con.execute("SELECT 1 FROM applications WHERE id=?", (application_id,)).fetchone()
        if not row:
            return False
        con.execute("DELETE FROM application_events WHERE application_id=?", (application_id,))
        con.execute("DELETE FROM applications WHERE id=?", (application_id,))
    return True

def _from_row(row: sqlite3.Row) -> JobApplication:
    return JobApplication(**dict(row))
