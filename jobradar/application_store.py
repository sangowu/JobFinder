"""SQLite persistence for email-derived job application status."""
from __future__ import annotations

import json
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
_MISSING_IDENTITY_VALUES = {
    "", "unknown", "unknown role", "unknown company", "n/a", "none",
    "not available", "not provided", "not specified", "unspecified",
}
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
CREATE TABLE IF NOT EXISTS email_classification_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_message_id TEXT NOT NULL,
    application_id INTEGER,
    body_hash TEXT NOT NULL DEFAULT '',
    trigger_reason TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT 'rules',
    llm_provider TEXT NOT NULL DEFAULT '',
    llm_model TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    disagreement INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    rule_json TEXT NOT NULL,
    llm_json TEXT,
    final_json TEXT NOT NULL,
    human_label_json TEXT,
    human_reviewed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(provider, provider_message_id)
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
        "CREATE INDEX IF NOT EXISTS idx_email_classification_application "
        "ON email_classification_observations(application_id);"
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


def _is_missing_identity(value: str) -> bool:
    return value.strip().casefold() in _MISSING_IDENTITY_VALUES


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
        raw_company = analysis.company.strip()
        raw_title = analysis.job_title.strip()
        company = "Unknown company" if _is_missing_identity(raw_company) else raw_company
        title = "Unknown role" if _is_missing_identity(raw_title) else raw_title
        company_key = _identity_key(company, company=True)
        title_key = _identity_key(title)
        row = None
        reference = (analysis.application_reference or "").strip()
        if reference:
            row = con.execute(
                "SELECT id, company, job_title, last_event_at FROM applications "
                "WHERE lower(external_reference)=lower(?) ORDER BY updated_at DESC LIMIT 1",
                (reference,),
            ).fetchone()
        if not row and thread_id:
            row = con.execute(
                "SELECT id, company, job_title, last_event_at FROM applications "
                "WHERE gmail_thread_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if not row and company != "Unknown company" and title != "Unknown role":
            row = con.execute(
                "SELECT id, company, job_title, last_event_at FROM applications "
                "WHERE company_key=? AND job_title_key=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (company_key, title_key),
            ).fetchone()
        if row:
            application_id = int(row["id"])
            merged_company = (
                company
                if _is_missing_identity(row["company"]) and not _is_missing_identity(company)
                else row["company"]
            )
            merged_title = (
                title
                if _is_missing_identity(row["job_title"]) and not _is_missing_identity(title)
                else row["job_title"]
            )
            merged_company_key = _identity_key(merged_company, company=True)
            merged_title_key = _identity_key(merged_title)
            if event_at.isoformat() >= row["last_event_at"]:
                con.execute(
                    "UPDATE applications SET current_status=?, company=?, job_title=?, "
                    "company_key=?, job_title_key=?, last_event_at=?, updated_at=?, "
                    "external_reference=COALESCE(NULLIF(external_reference, ''), ?), "
                    "gmail_thread_id=COALESCE(NULLIF(gmail_thread_id, ''), ?) WHERE id=?",
                    (analysis.status, merged_company, merged_title, merged_company_key,
                     merged_title_key, event_at.isoformat(), now, reference or None,
                     thread_id or None, application_id),
                )
            else:
                con.execute(
                    "UPDATE applications SET company=?, job_title=?, company_key=?, job_title_key=?, "
                    "external_reference=COALESCE(NULLIF(external_reference, ''), ?), "
                    "gmail_thread_id=COALESCE(NULLIF(gmail_thread_id, ''), ?), "
                    "updated_at=? WHERE id=?",
                    (merged_company, merged_title, merged_company_key, merged_title_key,
                     reference or None, thread_id or None, now, application_id),
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
    record_classification_feedback(
        application_id,
        is_job_related=True,
        status=status,
        company=company,
        job_title=job_title,
        action="confirmed",
    )
    return get_application(application_id)


def record_classification_observation(
    *, provider: str, message_id: str, application_id: int | None, body_hash: str,
    trigger_reason: str, decision: str, llm_provider: str, llm_model: str,
    latency_ms: int, input_tokens: int, output_tokens: int, disagreement: bool,
    error_message: str, rule_analysis: ApplicationEmailAnalysis,
    llm_analysis: ApplicationEmailAnalysis | None, final_analysis: ApplicationEmailAnalysis,
) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO email_classification_observations ("
            "provider, provider_message_id, application_id, body_hash, trigger_reason, decision, "
            "llm_provider, llm_model, latency_ms, input_tokens, output_tokens, disagreement, "
            "error_message, rule_json, llm_json, final_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider, message_id, application_id, body_hash, trigger_reason, decision,
                llm_provider, llm_model, latency_ms, input_tokens, output_tokens,
                int(disagreement), error_message[:500], rule_analysis.model_dump_json(),
                llm_analysis.model_dump_json() if llm_analysis else None,
                final_analysis.model_dump_json(), datetime.utcnow().isoformat(),
            ),
        )


def record_classification_feedback(
    application_id: int,
    *, is_job_related: bool, status: str = "unknown", company: str = "",
    job_title: str = "", action: str,
) -> None:
    label = json.dumps({
        "is_job_related": is_job_related,
        "status": status,
        "company": company.strip(),
        "job_title": job_title.strip(),
        "action": action,
    }, ensure_ascii=False)
    with _conn() as con:
        con.execute(
            "UPDATE email_classification_observations SET human_label_json=?, human_reviewed_at=? "
            "WHERE application_id=?",
            (label, datetime.utcnow().isoformat(), application_id),
        )


def get_classification_metrics() -> dict:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM email_classification_observations ORDER BY id"
        ).fetchall()
    observations = [dict(row) for row in rows]
    llm_rows = [row for row in observations if row["trigger_reason"]]
    reviewed = [row for row in observations if row["human_label_json"]]
    related_correct = 0
    status_evaluated = 0
    status_correct = 0
    confusion: dict[str, dict[str, int]] = {}
    for row in reviewed:
        final = json.loads(row["final_json"])
        prediction = json.loads(row["llm_json"]) if row["llm_json"] else final
        human = json.loads(row["human_label_json"])
        if bool(prediction.get("is_job_related")) == bool(human.get("is_job_related")):
            related_correct += 1
        predicted_status = prediction.get("status", "unknown")
        actual_status = human.get("status", "unknown")
        if human.get("is_job_related") and predicted_status != "unknown":
            status_evaluated += 1
            status_correct += int(predicted_status == actual_status)
            confusion.setdefault(predicted_status, {})[actual_status] = (
                confusion.setdefault(predicted_status, {}).get(actual_status, 0) + 1
            )
    return {
        "total_classified": len(observations),
        "llm_calls": len(llm_rows),
        "llm_failures": sum(bool(row["error_message"]) for row in llm_rows),
        "llm_pending": sum(row["decision"] == "llm_pending" for row in llm_rows),
        "llm_auto": sum(row["decision"] in {"llm_auto", "llm_unrelated"} for row in llm_rows),
        "disagreements": sum(int(row["disagreement"]) for row in llm_rows),
        "input_tokens": sum(int(row["input_tokens"]) for row in llm_rows),
        "output_tokens": sum(int(row["output_tokens"]) for row in llm_rows),
        "average_latency_ms": round(
            sum(int(row["latency_ms"]) for row in llm_rows) / len(llm_rows)
        ) if llm_rows else 0,
        "reviewed": len(reviewed),
        "related_accuracy": round(related_correct / len(reviewed), 3) if reviewed else None,
        "status_evaluated": status_evaluated,
        "status_accuracy": round(status_correct / status_evaluated, 3) if status_evaluated else None,
        "confusion": confusion,
    }

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
        runs = [dict(row) for row in rows]
        for run in runs:
            if run["trigger"] != "reanalysis":
                continue
            row = con.execute(
                "SELECT COUNT(*) AS count FROM applications "
                "WHERE created_at>=? AND created_at<=?",
                (run["started_at"], run["completed_at"]),
            ).fetchone()
            run["matched"] = int(row["count"])
    return runs


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
        con.execute("DELETE FROM email_classification_observations")
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
        con.execute(
            "UPDATE email_classification_observations SET human_label_json=?, human_reviewed_at=? "
            "WHERE application_id=?",
            (json.dumps({"is_job_related": False, "status": "unknown", "action": "discarded"}),
             datetime.utcnow().isoformat(), application_id),
        )
        con.execute("DELETE FROM application_events WHERE application_id=?", (application_id,))
        con.execute("DELETE FROM applications WHERE id=?", (application_id,))
    return True

def _from_row(row: sqlite3.Row) -> JobApplication:
    return JobApplication(**dict(row))
