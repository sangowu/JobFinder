"""SQLite persistence for email-derived job application status."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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
    processed_at TEXT NOT NULL,
    PRIMARY KEY (provider, provider_message_id)
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    company TEXT NOT NULL DEFAULT '',
    job_title TEXT NOT NULL DEFAULT '',
    current_status TEXT NOT NULL DEFAULT 'unknown',
    applied_at TEXT,
    last_event_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'email',
    external_reference TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    email_message_id TEXT NOT NULL DEFAULT '',
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
    error_message TEXT NOT NULL DEFAULT ''
);
"""


@contextmanager
def _conn():
    path = Path(os.getenv("CACHE_DB_PATH", _DEFAULT_DB_PATH))
    ensure_parent(path)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        con.executescript(_INIT_SQL)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def email_was_processed(provider: str, message_id: str) -> bool:
    with _conn() as con:
        return con.execute(
            "SELECT 1 FROM processed_emails WHERE provider=? AND provider_message_id=?",
            (provider, message_id),
        ).fetchone() is not None


def record_email(
    *, provider: str, message_id: str, received_at: datetime, sender: str,
    subject: str, body_hash: str, analysis: ApplicationEmailAnalysis,
) -> JobApplication | None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        if con.execute(
            "SELECT 1 FROM processed_emails WHERE provider=? AND provider_message_id=?",
            (provider, message_id),
        ).fetchone():
            return None
        con.execute(
            "INSERT INTO processed_emails VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (provider, message_id, received_at.isoformat(), sender, subject, body_hash,
             analysis.model_dump_json(), now),
        )
        if not analysis.is_job_related:
            return None
        event_at = analysis.event_at or received_at
        company = analysis.company.strip() or "Unknown company"
        title = analysis.job_title.strip() or "Unknown role"
        row = None
        if company != "Unknown company" and title != "Unknown role":
            row = con.execute(
                "SELECT id FROM applications WHERE lower(company)=lower(?) AND lower(job_title)=lower(?) "
                "ORDER BY updated_at DESC LIMIT 1",
                (company, title),
            ).fetchone()
        if row:
            application_id = int(row["id"])
            con.execute(
                "UPDATE applications SET current_status=?, last_event_at=?, updated_at=?, "
                "external_reference=COALESCE(external_reference, ?) WHERE id=?",
                (analysis.status, event_at.isoformat(), now, analysis.application_reference, application_id),
            )
        else:
            applied_at = event_at.isoformat() if analysis.status == "submitted" else None
            cursor = con.execute(
                "INSERT INTO applications (company, job_title, current_status, applied_at, last_event_at, "
                "external_reference, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (company, title, analysis.status, applied_at, event_at.isoformat(),
                 analysis.application_reference, now, now),
            )
            application_id = int(cursor.lastrowid)
        con.execute(
            "INSERT OR IGNORE INTO application_events (application_id, email_message_id, event_type, "
            "event_at, confidence, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (application_id, message_id, analysis.status, event_at.isoformat(),
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
            "UPDATE applications SET current_status=?, company=?, job_title=?, updated_at=?, "
            "last_event_at=? WHERE id=?",
            (status, company.strip(), job_title.strip(), now, now, application_id),
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
    scanned: int = 0, matched: int = 0, error_message: str = "",
) -> dict:
    with _conn() as con:
        cursor = con.execute(
            "INSERT INTO email_sync_runs (trigger, status, started_at, completed_at, duration_ms, "
            "candidates, already_processed, scanned, matched, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trigger, status, started_at.isoformat(), completed_at.isoformat(), duration_ms,
             candidates, already_processed, scanned, matched, error_message[:500]),
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
