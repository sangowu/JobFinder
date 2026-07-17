from __future__ import annotations

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


def test_application_tracker_loads_sync_history():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="email-sync-meta"' in html
    assert "/api/email/sync-history?limit=10" in html
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