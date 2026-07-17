"""Google OAuth and Gmail API synchronization for application emails."""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from jobradar import application_store
from jobradar.email_classifier import classify_application_email
from jobradar.paths import DATA_DIR, ensure_parent

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_PATH = DATA_DIR / "google_gmail_token.json"
_sync_lock = threading.Lock()


def google_oauth_configured() -> bool:
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))


def email_sync_configured() -> bool:
    return google_oauth_configured() and _TOKEN_PATH.exists()


def google_oauth_authorization_url(redirect_uri: str) -> tuple[str, str, str]:
    flow = _oauth_flow(redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return url, state, flow.code_verifier


def complete_google_oauth(*, code: str, redirect_uri: str, expected_state: str, code_verifier: str) -> None:
    flow = _oauth_flow(redirect_uri, state=expected_state, code_verifier=code_verifier)
    previous_relax = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(code=code)
    finally:
        if previous_relax is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous_relax
    granted = set(flow.credentials.granted_scopes or flow.credentials.scopes or [])
    if GMAIL_READONLY_SCOPE not in granted:
        raise RuntimeError("Google did not grant Gmail read-only access")
    ensure_parent(_TOKEN_PATH)
    _TOKEN_PATH.write_text(flow.credentials.to_json(), encoding="utf-8")


def disconnect_google_email() -> None:
    if _TOKEN_PATH.exists():
        _TOKEN_PATH.unlink()


def gmail_message_url(message_id: str) -> str:
    """Resolve a Gmail API message ID to its thread URL for the connected account."""
    credentials = _load_credentials()
    message = _gmail_get(credentials, f"/messages/{message_id}", params={"format": "minimal"})
    thread_id = message.get("threadId")
    if not thread_id:
        raise RuntimeError("Gmail message does not contain a thread ID")
    profile = _gmail_get(credentials, "/profile")
    email_address = profile.get("emailAddress", "")
    authuser = f"?authuser={quote(email_address)}" if email_address else ""
    return f"https://mail.google.com/mail/u/{authuser}#all/{thread_id}"

def sync_email(limit: int = 100, trigger: str = "manual") -> dict[str, int | str | bool]:
    started_at = datetime.utcnow()
    started_perf = time.perf_counter()
    metrics = {"candidates": 0, "already_processed": 0, "scanned": 0, "matched": 0}

    if not email_sync_configured():
        return _record_sync_result(
            trigger=trigger, status="failed", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message="Google email is not connected",
        )
    if not _sync_lock.acquire(blocking=False):
        return _record_sync_result(
            trigger=trigger, status="skipped", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message="Email sync is already running",
        )
    try:
        credentials = _load_credentials()
        response = _gmail_get(credentials, "/messages", params={
            "maxResults": max(1, min(limit, 500)), "q": "newer_than:30d",
        })
        messages = response.get("messages", [])
        metrics["candidates"] = len(messages)
        for item in messages:
            message_id = item["id"]
            if application_store.email_was_processed("gmail", message_id):
                metrics["already_processed"] += 1
                continue
            message = _gmail_get(credentials, f"/messages/{message_id}", params={"format": "full"})
            payload = message.get("payload", {})
            headers = {header["name"].lower(): header["value"] for header in payload.get("headers", [])}
            subject = _decode_header(headers.get("subject", ""))
            sender = _decode_header(headers.get("from", ""))
            received_at = _message_datetime(headers.get("date", ""), message.get("internalDate"))
            body = _payload_text(payload)
            analysis = classify_application_email(
                subject=subject, body=body, received_at=received_at, sender=sender, headers=headers,
            )
            result = application_store.record_email(
                provider="gmail", message_id=message_id, received_at=received_at,
                sender=sender, subject=subject,
                body_hash=hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest(),
                analysis=analysis,
            )
            metrics["scanned"] += 1
            if result is not None:
                metrics["matched"] += 1
        return _record_sync_result(
            trigger=trigger, status="success", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message="Email sync complete",
        )
    except Exception as exc:
        _record_sync_result(
            trigger=trigger, status="failed", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message=str(exc),
        )
        raise
    finally:
        _sync_lock.release()


def _record_sync_result(
    *, trigger: str, status: str, started_at: datetime, started_perf: float,
    metrics: dict[str, int], message: str,
) -> dict[str, int | str | bool]:
    completed_at = datetime.utcnow()
    duration_ms = round((time.perf_counter() - started_perf) * 1000)
    run = application_store.record_sync_run(
        trigger=trigger, status=status, started_at=started_at, completed_at=completed_at,
        duration_ms=duration_ms, error_message=message if status == "failed" else "",
        **metrics,
    )
    return {
        "ok": status == "success",
        "message": message,
        "run_id": run["id"],
        "status": status,
        "duration_ms": duration_ms,
        **metrics,
    }

def _oauth_flow(redirect_uri: str, state: str | None = None, code_verifier: str | None = None) -> Flow:
    if not google_oauth_configured():
        raise RuntimeError("Google OAuth client is not configured")
    config = {"web": {
        "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect_uri],
    }}
    flow = Flow.from_client_config(
        config,
        scopes=[GMAIL_READONLY_SCOPE],
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=code_verifier is None,
    )
    flow.redirect_uri = redirect_uri
    return flow


def _load_credentials() -> Credentials:
    credentials = Credentials.from_authorized_user_file(str(_TOKEN_PATH), [GMAIL_READONLY_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        _TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise RuntimeError("Google authorization is invalid; reconnect the account")
    return credentials


def _gmail_get(credentials: Credentials, path: str, params: dict | None = None) -> dict:
    response = requests.get(
        f"{_GMAIL_API}{path}", params=params,
        headers={"Authorization": f"Bearer {credentials.token}"}, timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _message_datetime(date_header: str, internal_date: str | None) -> datetime:
    try:
        value = parsedate_to_datetime(date_header)
        if value is not None:
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    if internal_date:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc).replace(tzinfo=None)
    return datetime.utcnow()


def _payload_text(payload: dict) -> str:
    texts: list[str] = []
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        texts.append(_decode_body(payload["body"]["data"]))
    for part in payload.get("parts", []):
        texts.append(_payload_text(part))
    return "\n".join(text for text in texts if text)


def _decode_body(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")
