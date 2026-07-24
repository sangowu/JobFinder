from __future__ import annotations

import asyncio
import importlib
import os
from datetime import datetime

import pytest

from jobradar.email_classifier import classify_application_email
from jobradar.schemas import ApplicationEmailAnalysis


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "applications.db"))
    import jobradar.application_store as store_mod

    importlib.reload(store_mod)
    return store_mod


def test_classifier_ignores_unrelated_email():
    result = classify_application_email(
        subject="Your monthly statement",
        body="Your account statement is ready.",
        received_at=datetime(2026, 7, 17),
    )
    assert result.is_job_related is False
    assert result.status == "unknown"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Thank you for applying", "submitted"),
        ("Coding challenge for your application", "assessment"),
        ("Interview invitation", "interview"),
        ("Unfortunately, we are not moving forward", "rejected"),
        ("Your job offer", "offer"),
    ],
)
def test_classifier_recognizes_status(subject, expected):
    result = classify_application_email(
        subject=subject,
        body="Position: AI Engineer\nCompany: Acme",
        received_at=datetime(2026, 7, 17),
    )
    assert result.is_job_related is True
    assert result.status == expected
    assert result.company == "Acme"
    assert result.job_title == "AI Engineer"


@pytest.mark.parametrize(
    ("subject", "headers"),
    [
        ("Your weekly job alert: 12 new jobs matching your profile", {}),
        ("Career opportunities from Workday", {"List-Unsubscribe": "<https://example.test/unsubscribe>"}),
        ("Latest vacancies for AI roles", {"Precedence": "bulk"}),
    ],
)
def test_classifier_filters_ats_subscription_email(subject, headers):
    result = classify_application_email(
        subject=subject,
        body="Browse these recommended positions and update your job alert preferences.",
        received_at=datetime(2026, 7, 18),
        headers=headers,
    )
    assert result.is_job_related is False
    assert result.status == "unknown"


def test_classifier_keeps_transactional_email_with_unsubscribe_header():
    result = classify_application_email(
        subject="Thank you for applying",
        body="Your application for AI Engineer at Acme has been received.",
        received_at=datetime(2026, 7, 18),
        headers={"List-Unsubscribe": "<https://example.test/unsubscribe>"},
    )
    assert result.is_job_related is True
    assert result.status == "submitted"


def test_classifier_keeps_icims_application_receipt_with_unsubscribe_header():
    result = classify_application_email(
        subject="Thank You for Your Application with Docusign",
        body="Thank you for your interest in Docusign.",
        sender='"Docusign @ icims" <docusign+autoreply@talent.icims.com>',
        received_at=datetime(2026, 7, 21),
        headers={"List-Unsubscribe": "<https://talent.icims.com/unsubscribe>"},
    )

    assert result.is_job_related is True
    assert result.status == "submitted"
    assert result.company == "Docusign"
    assert result.classification_reason == "transactional:submitted"
    assert result.classifier_version == "rules-v3"


def test_classifier_marks_bulk_header_without_subscription_content_as_uncertain():
    result = classify_application_email(
        subject="An update from the hiring team",
        body="There is an update regarding your application.",
        received_at=datetime(2026, 7, 21),
        headers={"List-Unsubscribe": "<https://example.test/unsubscribe>"},
    )

    assert result.is_job_related is True
    assert result.status == "unknown"
    assert result.classification_reason == "bulk_header_uncertain:list-unsubscribe"


@pytest.mark.parametrize(
    "subject",
    [
        "Your latest jobs from gradireland",
        "Michael Madden and others share their thoughts on LinkedIn",
        "Better Work: How hiring has changed",
    ],
)
def test_subscription_subject_overrides_transactional_words_in_digest_body(subject):
    result = classify_application_email(
        subject=subject,
        body="This digest discusses interviews, coding challenges and candidates not moving forward.",
        received_at=datetime(2026, 7, 18),
    )
    assert result.is_job_related is False
    assert result.classification_reason.startswith("subscription:content:")

def test_store_is_idempotent_and_builds_timeline(store):
    received = datetime(2026, 7, 17, 10, 30)
    submitted = ApplicationEmailAnalysis(
        is_job_related=True,
        status="submitted",
        company="Acme",
        job_title="AI Engineer",
        event_at=received,
        confidence=0.9,
        summary="Application received",
    )
    first = store.record_email(
        provider="test", message_id="1", received_at=received, sender="jobs@acme.test",
        subject="Application received", body_hash="abc", analysis=submitted,
    )
    duplicate = store.record_email(
        provider="test", message_id="1", received_at=received, sender="jobs@acme.test",
        subject="Application received", body_hash="abc", analysis=submitted,
    )
    rejected = submitted.model_copy(update={"status": "rejected", "summary": "Not moving forward"})
    store.record_email(
        provider="test", message_id="2", received_at=received, sender="jobs@acme.test",
        subject="Update", body_hash="def", analysis=rejected,
    )

    assert first is not None
    assert duplicate is None
    applications = store.list_applications()
    assert len(applications) == 1
    assert applications[0].current_status == "rejected"
    detail = store.get_application(applications[0].id)
    assert detail is not None
    assert len(detail.events) == 2


def test_manual_update(store):
    analysis = ApplicationEmailAnalysis(
        is_job_related=True, status="unknown", company="Unknown company",
        job_title="Unknown role", confidence=0.55,
    )
    item = store.record_email(
        provider="test", message_id="3", received_at=datetime(2026, 7, 17), sender="",
        subject="Application update", body_hash="ghi", analysis=analysis,
    )
    updated = store.update_application(item.id, status="interview", company="Example", job_title="ML Engineer")
    assert updated.company == "Example"
    assert updated.current_status == "interview"
    assert any(event.event_type == "interview" for event in updated.events)


def test_google_oauth_authorization_url(monkeypatch):
    from jobradar.email_sync import google_oauth_authorization_url

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    url, state, verifier = google_oauth_authorization_url(
        "http://127.0.0.1:8765/api/email/google/callback"
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "gmail.readonly" in url
    assert state


def test_gmail_payload_decoding():
    from jobradar.email_sync import _payload_text

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/plain", "body": {"data": "SGVsbG8gd29ybGQ"}}],
    }
    assert _payload_text(payload) == "Hello world"


def test_gmail_message_url_uses_thread_and_connected_account(monkeypatch):
    import jobradar.email_sync as email_sync

    monkeypatch.setattr(email_sync, "_load_credentials", lambda: object())
    responses = {
        "/messages/message-1": {"threadId": "thread-9"},
        "/profile": {"emailAddress": "person+jobs@gmail.com"},
    }
    monkeypatch.setattr(
        email_sync, "_gmail_get", lambda credentials, path, params=None: responses[path]
    )

    assert email_sync.gmail_message_url("message-1") == (
        "https://mail.google.com/mail/u/?authuser=person%2Bjobs%40gmail.com#all/thread-9"
    )

def test_jobs_route_remains_bound_to_get_jobs():
    from jobradar.server import app

    route = next(route for route in app.routes if route.path == "/api/jobs")
    assert route.endpoint.__name__ == "get_jobs"


def test_application_nav_button_is_not_nested_in_config_button():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    config_start = html.index('<button onclick="openConfigPage()"')
    config_end = html.index("</button>", config_start)
    config_button = html[config_start:config_end]
    assert 'onclick="openApplicationsPage()"' not in config_button


def test_application_script_is_inside_main_script_block():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    function_position = html.index("async function openApplicationsPage()")
    script_close = html.rindex("</script>")
    html_close = html.rindex("</html>")
    assert function_position < script_close < html_close
    assert not html[html_close + len("</html>"):].strip()

def test_google_oauth_url_does_not_merge_prior_grants(monkeypatch):
    from jobradar.email_sync import google_oauth_authorization_url

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    url, _, _ = google_oauth_authorization_url("http://127.0.0.1:8765/api/email/google/callback")
    assert "include_granted_scopes" not in url


def test_google_oauth_accepts_scope_superset_and_cleans_environment(tmp_path, monkeypatch):
    import jobradar.email_sync as email_sync

    class FakeCredentials:
        granted_scopes = [email_sync.GMAIL_READONLY_SCOPE, "openid", "email"]
        scopes = None

        @staticmethod
        def to_json():
            return "{}"

    class FakeFlow:
        credentials = FakeCredentials()

        @staticmethod
        def fetch_token(**kwargs):
            assert kwargs["code"] == "redacted-code"
            assert os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "1"

    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)
    monkeypatch.setattr(email_sync, "_oauth_flow", lambda *args, **kwargs: FakeFlow())
    monkeypatch.setattr(email_sync, "_TOKEN_PATH", tmp_path / "token.json")
    email_sync.complete_google_oauth(
        code="redacted-code",
        redirect_uri="http://127.0.0.1/callback",
        expected_state="state",
        code_verifier="verifier",
    )
    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in os.environ
    assert (tmp_path / "token.json").exists()

def test_google_connect_endpoint_preserves_pkce_verifier(monkeypatch):
    from fastapi.testclient import TestClient
    import jobradar.server as server

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    server._google_oauth_state = None
    server._google_oauth_code_verifier = None
    response = TestClient(server.app).post("/api/email/google/connect")
    assert response.status_code == 200
    assert server._google_oauth_state
    assert server._google_oauth_code_verifier
    assert len(server._google_oauth_code_verifier) == 128

def test_application_renderer_uses_existing_escape_helper():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    start = html.index("function renderApplications()")
    end = html.index("window.addEventListener('message'", start)
    renderer = html[start:end]
    assert "${esc(" not in renderer
    assert "${_esc(" in renderer

def test_sync_run_history_roundtrip(store):
    started = datetime(2026, 7, 18, 9, 0)
    completed = datetime(2026, 7, 18, 9, 0, 2)
    run = store.record_sync_run(
        trigger="manual", status="success", started_at=started, completed_at=completed,
        duration_ms=2000, candidates=100, already_processed=90, scanned=10, matched=4,
    )
    assert run["id"]
    assert run["candidates"] == 100
    assert run["already_processed"] == 90
    assert run["matched"] == 4
    assert store.latest_sync_run()["id"] == run["id"]
    assert store.list_sync_runs(10) == [run]


def test_reanalysis_history_reports_unique_applications(store):
    from datetime import timedelta

    started = datetime.utcnow() - timedelta(seconds=1)
    analysis = ApplicationEmailAnalysis(
        is_job_related=True,
        status="submitted",
        company="Acme",
        job_title="AI Engineer",
        event_at=started,
        confidence=0.9,
    )
    store.record_email(
        provider="gmail",
        message_id="receipt-1",
        received_at=started,
        sender="jobs@acme.test",
        subject="Application received",
        body_hash="hash-1",
        analysis=analysis,
        thread_id="thread-1",
    )
    store.record_email(
        provider="gmail",
        message_id="receipt-2",
        received_at=started,
        sender="jobs@acme.test",
        subject="Application confirmed",
        body_hash="hash-2",
        analysis=analysis,
        thread_id="thread-1",
    )
    completed = datetime.utcnow() + timedelta(seconds=1)
    store.record_sync_run(
        trigger="reanalysis",
        status="success",
        started_at=started,
        completed_at=completed,
        duration_ms=100,
        job_related=2,
        matched=2,
    )

    run = store.list_sync_runs(1)[0]
    assert run["job_related"] == 2
    assert run["matched"] == 1


def test_application_tracker_loads_sync_history():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="email-sync-meta"' in html
    assert "/api/email/sync-history?limit=10" in html
    assert 'id="email-sync-history" class="space-y-2"' in html
    assert "toggleEmailSyncRun(runId)" in html
    assert "<tbody id=\"email-sync-history\"" not in html
    sync_start = html.index("async function syncApplicationEmail()")
    sync_end = html.index("async function loadApplications()", sync_start)
    assert "loadEmailSyncHistory()" in html[sync_start:sync_end]

def test_discard_application_keeps_email_processed(store):
    analysis = ApplicationEmailAnalysis(
        is_job_related=True, status="unknown", company="Unknown company",
        job_title="Unknown role", confidence=0.55,
    )
    item = store.record_email(
        provider="test", message_id="discard-1", received_at=datetime(2026, 7, 18),
        sender="", subject="Possible application", body_hash="hash", analysis=analysis,
    )
    assert store.delete_application(item.id) is True
    assert store.get_application(item.id) is None
    assert store.email_was_processed("test", "discard-1") is True
    assert store.delete_application(item.id) is False


def test_unknown_application_actions_are_rendered():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "reviewApplication(event" in html
    assert "discardApplication(event" in html
    assert "event.stopPropagation()" in html
    assert '@app.delete("/api/applications/' not in html


def test_application_row_opens_source_email():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "openApplicationEmail(" in html
    assert "/api/applications/${id}/email" in html


def test_older_email_cannot_regress_current_status(store):
    newer = ApplicationEmailAnalysis(
        is_job_related=True, status="rejected", company="Acme", job_title="AI Engineer",
        event_at=datetime(2026, 7, 20), confidence=0.9,
    )
    older = newer.model_copy(update={"status": "submitted", "event_at": datetime(2026, 7, 10)})
    store.record_email(
        provider="gmail", message_id="newer", thread_id="thread-1",
        received_at=newer.event_at, sender="", subject="Rejected", body_hash="new", analysis=newer,
    )
    store.record_email(
        provider="gmail", message_id="older", thread_id="thread-1",
        received_at=older.event_at, sender="", subject="Submitted", body_hash="old", analysis=older,
    )
    item = store.list_applications()[0]
    assert item.current_status == "rejected"
    assert item.last_event_at == datetime(2026, 7, 20)
    assert len(store.get_application(item.id).events) == 2


def test_application_merges_by_reference_before_company_title(store):
    first = ApplicationEmailAnalysis(
        is_job_related=True, status="submitted", company="Acme Ltd",
        job_title="Engineer", application_reference="REQ-123", confidence=0.9,
    )
    second = first.model_copy(update={
        "status": "interview", "company": "Acme", "job_title": "Software Engineer",
    })
    store.record_email(
        provider="gmail", message_id="ref-1", received_at=datetime(2026, 7, 10),
        sender="", subject="Applied", body_hash="1", analysis=first,
    )
    store.record_email(
        provider="gmail", message_id="ref-2", received_at=datetime(2026, 7, 11),
        sender="", subject="Interview", body_hash="2", analysis=second,
    )
    assert len(store.list_applications()) == 1
    assert store.list_applications()[0].current_status == "interview"


def test_application_merges_unknown_messages_by_gmail_thread(store):
    analysis = ApplicationEmailAnalysis(
        is_job_related=True, status="unknown", company="", job_title="", confidence=0.55,
    )
    for index in (1, 2):
        store.record_email(
            provider="gmail", message_id=f"thread-{index}", thread_id="shared-thread",
            received_at=datetime(2026, 7, 10 + index), sender="", subject="Update",
            body_hash=str(index), analysis=analysis,
        )
    assert len(store.list_applications()) == 1


def test_application_merges_normalized_company_identity(store):
    base = ApplicationEmailAnalysis(
        is_job_related=True, status="submitted", company="Acme Ltd.",
        job_title="AI  Engineer", confidence=0.9,
    )
    follow_up = base.model_copy(update={"status": "assessment", "company": "ACME"})
    store.record_email(
        provider="gmail", message_id="normalized-1", received_at=datetime(2026, 7, 10),
        sender="", subject="Applied", body_hash="1", analysis=base,
    )
    store.record_email(
        provider="gmail", message_id="normalized-2", received_at=datetime(2026, 7, 11),
        sender="", subject="Assessment", body_hash="2", analysis=follow_up,
    )
    assert len(store.list_applications()) == 1


def test_classifier_reports_reason_version_and_reference():
    result = classify_application_email(
        subject="Thank you for applying - Application ID: REQ-789",
        body="Position: AI Engineer\nCompany: Acme",
        received_at=datetime(2026, 7, 18),
    )
    assert result.classification_reason == "transactional:submitted"
    assert result.classifier_version == "rules-v3"
    assert result.application_reference == "REQ-789"


def test_gmail_html_only_payload_is_converted_to_text():
    import base64
    from jobradar.email_sync import _payload_text

    html = "<html><style>.x{}</style><body><p>Interview invitation</p><script>x()</script></body></html>"
    encoded = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
    result = _payload_text({"mimeType": "text/html", "body": {"data": encoded}})
    assert result == "Interview invitation"


def test_gmail_pagination_collects_all_pages(monkeypatch):
    import jobradar.email_sync as email_sync

    calls = []

    def fake_get(credentials, path, params=None):
        calls.append(dict(params))
        if params.get("pageToken") == "next":
            return {"messages": [{"id": "3"}]}
        return {"messages": [{"id": "1"}, {"id": "2"}], "nextPageToken": "next"}

    monkeypatch.setattr(email_sync, "_gmail_get", fake_get)
    ids, pages = email_sync._list_recent_message_ids(object(), 10)
    assert ids == ["1", "2", "3"]
    assert pages == 2
    assert calls[1]["pageToken"] == "next"


def test_gmail_history_collects_added_messages(monkeypatch):
    import jobradar.email_sync as email_sync

    monkeypatch.setattr(email_sync, "_gmail_get", lambda *args, **kwargs: {
        "historyId": "102",
        "history": [{"messagesAdded": [
            {"message": {"id": "message-2"}}, {"message": {"id": "message-2"}},
        ]}],
    })
    ids, history_id, pages = email_sync._list_history_message_ids(object(), "100", 10)
    assert ids == ["message-2"]
    assert history_id == "102"
    assert pages == 1


def test_full_sync_processes_messages_chronologically_and_saves_cursor(store, monkeypatch):
    import base64
    import jobradar.email_sync as email_sync

    monkeypatch.setattr(email_sync, "email_sync_configured", lambda: True)
    monkeypatch.setattr(email_sync, "_load_credentials", lambda: object())

    def message(message_id, timestamp, subject, body):
        return {
            "id": message_id,
            "threadId": "thread-1",
            "historyId": str(timestamp),
            "internalDate": str(timestamp * 1000),
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": subject}],
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")},
            },
        }

    responses = {
        "/messages/new": message(
            "new", 200, "Unfortunately, we are not moving forward",
            "Position: AI Engineer\nCompany: Acme",
        ),
        "/messages/old": message(
            "old", 100, "Thank you for applying",
            "Position: AI Engineer\nCompany: Acme",
        ),
    }

    def fake_get(credentials, path, params=None):
        if path == "/messages":
            return {"messages": [{"id": "new"}, {"id": "old"}]}
        if path == "/profile":
            return {"historyId": "300"}
        return responses[path]

    monkeypatch.setattr(email_sync, "_gmail_get", fake_get)
    result = email_sync.sync_email(limit=10, force_full=True)
    assert result["sync_mode"] == "full"
    assert result["job_related"] == 2
    assert result["matched"] == 1
    application = store.list_applications()[0]
    assert len(store.get_application(application.id).events) == 2
    assert application.current_status == "rejected"
    assert store.get_sync_state()["history_id"] == "300"


def test_full_sync_reports_incremental_progress(store, monkeypatch):
    import jobradar.email_sync as email_sync

    monkeypatch.setattr(email_sync, "email_sync_configured", lambda: True)
    monkeypatch.setattr(email_sync, "_load_credentials", lambda: object())
    monkeypatch.setattr(
        email_sync,
        "_list_recent_message_ids",
        lambda credentials, limit: (["message-1"], 1),
    )
    monkeypatch.setattr(email_sync, "_gmail_get", lambda *args, **kwargs: {"historyId": "300"})
    monkeypatch.setattr(
        email_sync,
        "_fetch_message",
        lambda *args, **kwargs: {
            "message_id": "message-1",
            "thread_id": "thread-1",
            "history_id": "299",
            "headers": {},
            "subject": "Thank you for applying",
            "sender": "Acme",
            "received_at": datetime(2026, 7, 24),
            "body": "Position: AI Engineer\nCompany: Acme",
        },
    )
    updates = []

    result = email_sync.sync_email(
        limit=10,
        force_full=True,
        progress_callback=lambda progress: updates.append(progress.copy()),
    )

    assert result["matched"] == 1
    assert [update["stage"] for update in updates] == [
        "starting", "listing", "fetching", "fetching", "analysing", "analysing", "finalising",
    ]
    assert updates[-2]["scanned"] == 1
    assert updates[-2]["matched"] == 1


def test_sync_state_pause_history_and_lease(store):
    assert store.get_sync_state()["paused"] == 0
    assert store.set_sync_paused(True)["paused"] == 1
    store.set_history_id("500")
    assert store.get_sync_state()["history_id"] == "500"
    assert store.acquire_sync_lease("owner-1") is True
    assert store.acquire_sync_lease("owner-2") is False
    store.release_sync_lease("owner-1")
    assert store.acquire_sync_lease("owner-2") is True


def test_existing_email_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    import jobradar.application_store as store_mod

    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE processed_emails (provider TEXT, provider_message_id TEXT, "
        "received_at TEXT, sender TEXT DEFAULT '', subject TEXT DEFAULT '', "
        "body_hash TEXT DEFAULT '', analysis_json TEXT, processed_at TEXT, "
        "PRIMARY KEY (provider, provider_message_id))"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("CACHE_DB_PATH", str(path))
    importlib.reload(store_mod)
    store_mod.get_sync_state()
    con = sqlite3.connect(path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(processed_emails)")}
    con.close()
    assert {"gmail_thread_id", "classification_reason", "classifier_version"} <= columns


def test_clear_email_data_can_pause_and_reset_cursor(store):
    store.set_history_id("500")
    store.record_sync_run(
        trigger="manual", status="success", started_at=datetime(2026, 7, 18),
        completed_at=datetime(2026, 7, 18), duration_ms=1,
    )
    store.clear_email_data(pause=True)
    assert store.list_applications() == []
    assert store.list_sync_runs() == []
    state = store.get_sync_state()
    assert state["paused"] == 1
    assert state["history_id"] == ""


def test_application_tracker_has_email_data_controls():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "/api/email/pause" in html
    assert "/api/email/resume" in html
    assert "/api/email/data?pause=true" in html
    assert "/api/email/reanalyse" in html
    assert "/api/email/reanalyse/progress" in html


def test_email_reanalysis_background_state_completes(monkeypatch):
    import jobradar.server as server

    def fake_reanalyse(progress_callback):
        progress_callback({
            "stage": "analysing",
            "candidates": 3,
            "scanned": 2,
            "matched": 1,
        })
        return {
            "ok": True,
            "message": "Email sync complete",
            "scanned": 3,
            "matched": 2,
        }

    monkeypatch.setattr(server, "reanalyse_email", fake_reanalyse)
    server._email_reanalysis_state = {"status": "running", "stage": "starting"}

    asyncio.run(server._run_email_reanalysis())

    assert server._email_reanalysis_state["status"] == "completed"
    assert server._email_reanalysis_state["stage"] == "completed"
    assert server._email_reanalysis_state["scanned"] == 3
    assert server._email_reanalysis_state["matched"] == 2
