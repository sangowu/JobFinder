"""Google OAuth and Gmail API synchronization for application emails."""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from jobradar import application_store
from jobradar.email_classifier import classify_application_email
from jobradar.email_llm_classifier import classify_ambiguous_email
from jobradar.logger import get_logger
from jobradar.metrics import (
    EMAIL_SYNC_DURATION_SECONDS,
    EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS,
    EMAIL_SYNC_RUNS,
)
from jobradar.paths import DATA_DIR, ensure_parent

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_PATH = DATA_DIR / "google_gmail_token.json"
_sync_lock = threading.Lock()
_DEFAULT_MAX_MESSAGES = 5000
_DEFAULT_FETCH_WORKERS = 8
_MAX_FETCH_WORKERS = 16
_DEFAULT_ANALYSIS_WORKERS = 3
_MAX_ANALYSIS_WORKERS = 8
logger = get_logger("email_sync")


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
    return _gmail_thread_url(credentials, thread_id)


def gmail_thread_url(thread_id: str) -> str:
    return _gmail_thread_url(_load_credentials(), thread_id)


def _gmail_thread_url(credentials: Credentials, thread_id: str) -> str:
    profile = _gmail_get(credentials, "/profile")
    email_address = profile.get("emailAddress", "")
    authuser = f"?authuser={quote(email_address)}" if email_address else ""
    return f"https://mail.google.com/mail/u/{authuser}#all/{thread_id}"


def set_automatic_sync_paused(paused: bool) -> dict:
    return application_store.set_sync_paused(paused)


def clear_email_tracking_data(*, pause: bool = True) -> None:
    if not _sync_lock.acquire(blocking=False):
        raise RuntimeError("Email sync is already running")
    owner = f"control:{os.getpid()}:{uuid.uuid4().hex}"
    try:
        if not application_store.acquire_sync_lease(owner):
            raise RuntimeError("Email sync is running in another process")
        application_store.clear_email_data(reset_history=True, pause=pause)
    finally:
        application_store.release_sync_lease(owner)
        _sync_lock.release()


EmailSyncProgressCallback = Callable[[dict[str, int | str]], None]


def reanalyse_email(
    progress_callback: EmailSyncProgressCallback | None = None,
) -> dict[str, int | str | bool]:
    return sync_email(
        trigger="reanalysis",
        force_full=True,
        reset_data=True,
        progress_callback=progress_callback,
    )

def sync_email(
    limit: int | None = None,
    trigger: str = "manual",
    *,
    force_full: bool = False,
    reset_data: bool = False,
    progress_callback: EmailSyncProgressCallback | None = None,
) -> dict[str, int | str | bool]:
    started_at = datetime.utcnow()
    started_perf = time.perf_counter()
    metrics = {
        "candidates": 0, "already_processed": 0, "scanned": 0, "matched": 0,
        "pages": 0, "job_related": 0, "subscription_filtered": 0, "unrelated": 0,
        "unknown": 0, "failed_messages": 0,
    }
    sync_mode = "full"
    history_id = ""
    fetched = 0
    analysed = 0
    matched_application_ids: set[int] = set()

    def emit_progress(stage: str) -> None:
        if progress_callback is not None:
            progress_callback({
                "stage": stage, "fetched": fetched, "analysed": analysed, **metrics,
            })

    emit_progress("starting")
    if not email_sync_configured():
        return _record_sync_result(
            trigger=trigger, status="failed", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message="Google email is not connected", sync_mode=sync_mode,
        )
    if trigger == "scheduled" and application_store.get_sync_state()["paused"]:
        return _record_sync_result(
            trigger=trigger, status="skipped", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message="Automatic email sync is paused", sync_mode=sync_mode,
        )
    if not _sync_lock.acquire(blocking=False):
        return _record_sync_result(
            trigger=trigger, status="skipped", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message="Email sync is already running", sync_mode=sync_mode,
        )
    owner = f"{os.getpid()}:{uuid.uuid4().hex}"
    try:
        if not application_store.acquire_sync_lease(owner):
            return _record_sync_result(
                trigger=trigger, status="skipped", started_at=started_at,
                started_perf=started_perf, metrics=metrics,
                message="Email sync is running in another process", sync_mode=sync_mode,
            )
        if reset_data:
            application_store.clear_email_data(reset_history=True)
        emit_progress("listing")
        credentials = _load_credentials()
        max_messages = limit or int(os.getenv("EMAIL_SYNC_MAX_MESSAGES", str(_DEFAULT_MAX_MESSAGES)))
        state = application_store.get_sync_state()
        if state["history_id"] and not force_full:
            try:
                message_ids, history_id, pages = _list_history_message_ids(
                    credentials, state["history_id"], max_messages
                )
                sync_mode = "history"
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                message_ids, pages = _list_recent_message_ids(credentials, max_messages)
                history_id = _gmail_get(credentials, "/profile").get("historyId", "")
                sync_mode = "full_recovery"
        else:
            message_ids, pages = _list_recent_message_ids(credentials, max_messages)
            history_id = _gmail_get(credentials, "/profile").get("historyId", "")
        metrics["pages"] = pages
        metrics["candidates"] = len(message_ids)
        emit_progress("fetching")
        pending: list[dict] = []
        pending_ids = [
            message_id for message_id in message_ids
            if not application_store.email_was_processed("gmail", message_id)
        ]
        metrics["already_processed"] = len(message_ids) - len(pending_ids)
        fetch_workers = _fetch_worker_count()
        logger.info(
            "Gmail message fetch | messages=%d workers=%d",
            len(pending_ids), fetch_workers,
        )
        with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
            futures = {
                executor.submit(_fetch_message, credentials, message_id): message_id
                for message_id in pending_ids
            }
            for future in as_completed(futures):
                message_id = futures[future]
                try:
                    pending.append(future.result())
                    fetched += 1
                except Exception as exc:
                    metrics["failed_messages"] += 1
                    logger.warning("Gmail message fetch failed | id=%s error=%s", message_id, exc)
                emit_progress("fetching")
        ordered_pending = sorted(pending, key=lambda value: value["received_at"])
        analysis_workers = _analysis_worker_count()
        logger.info(
            "Email classification | messages=%d workers=%d",
            len(ordered_pending), analysis_workers,
        )
        emit_progress("analysing")
        analysed_messages: dict[int, tuple] = {}
        with ThreadPoolExecutor(max_workers=analysis_workers) as executor:
            futures = {
                executor.submit(_analyse_message, item): index
                for index, item in enumerate(ordered_pending)
            }
            for future in as_completed(futures):
                analysed_messages[futures[future]] = future.result()
                analysed += 1
                emit_progress("analysing")
        for index in range(len(ordered_pending)):
            item, rule_analysis, hybrid, analysis, body_hash = analysed_messages[index]
            result = application_store.record_email(
                provider="gmail", message_id=item["message_id"],
                received_at=item["received_at"], sender=item["sender"],
                subject=item["subject"], thread_id=item["thread_id"],
                history_id=item["history_id"],
                body_hash=body_hash, analysis=analysis,
            )
            try:
                application_store.record_classification_observation(
                    provider="gmail", message_id=item["message_id"],
                    application_id=result.id if result else None, body_hash=body_hash,
                    trigger_reason=hybrid.trigger_reason, decision=hybrid.decision,
                    llm_provider=hybrid.provider, llm_model=hybrid.model,
                    latency_ms=hybrid.latency_ms, input_tokens=hybrid.input_tokens,
                    output_tokens=hybrid.output_tokens, disagreement=hybrid.disagreement,
                    error_message=hybrid.error, rule_analysis=rule_analysis,
                    llm_analysis=hybrid.llm_analysis, final_analysis=analysis,
                )
            except Exception as exc:
                logger.warning(
                    "Email classification observation failed | id=%s error=%s",
                    item["message_id"], exc,
                )
            if hybrid.trigger_reason:
                logger.info(
                    "Email LLM classification | id=%s trigger=%s decision=%s provider=%s "
                    "model=%s latency_ms=%d tokens=%d/%d disagreement=%s error=%s",
                    item["message_id"], hybrid.trigger_reason, hybrid.decision,
                    hybrid.provider, hybrid.model, hybrid.latency_ms,
                    hybrid.input_tokens, hybrid.output_tokens, hybrid.disagreement,
                    hybrid.error or "none",
                )
            metrics["scanned"] += 1
            if analysis.is_job_related:
                metrics["job_related"] += 1
                if analysis.status == "unknown":
                    metrics["unknown"] += 1
            elif analysis.classification_reason.startswith("subscription:"):
                metrics["subscription_filtered"] += 1
            else:
                metrics["unrelated"] += 1
            if result is not None:
                matched_application_ids.add(result.id)
                metrics["matched"] = len(matched_application_ids)
            emit_progress("analysing")
        if not metrics["failed_messages"] and history_id:
            application_store.set_history_id(history_id)
        emit_progress("finalising")
        return _record_sync_result(
            trigger=trigger, status="success", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message="Email sync complete", sync_mode=sync_mode,
            history_id=history_id,
        )
    except Exception as exc:
        _record_sync_result(
            trigger=trigger, status="failed", started_at=started_at, started_perf=started_perf,
            metrics=metrics, message=str(exc), sync_mode=sync_mode, history_id=history_id,
        )
        raise
    finally:
        application_store.release_sync_lease(owner)
        _sync_lock.release()


def _record_sync_result(
    *, trigger: str, status: str, started_at: datetime, started_perf: float,
    metrics: dict[str, int], message: str, sync_mode: str, history_id: str = "",
) -> dict[str, int | str | bool]:
    completed_at = datetime.utcnow()
    duration_seconds = time.perf_counter() - started_perf
    duration_ms = round(duration_seconds * 1000)
    run = application_store.record_sync_run(
        trigger=trigger,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        error_message=message if status == "failed" else "",
        sync_mode=sync_mode,
        history_id=history_id,
        **metrics,
    )

    EMAIL_SYNC_RUNS.labels(
        trigger=trigger,
        status=status,
    ).inc()

    EMAIL_SYNC_DURATION_SECONDS.labels(
        trigger=trigger,
        status=status,
    ).observe(duration_seconds)

    if status == "success":
        EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS.set(time.time())

    return {
        "ok": status == "success",
        "message": message,
        "run_id": run["id"],
        "status": status,
        "duration_ms": duration_ms,
        "sync_mode": sync_mode,
        "history_id": history_id,
        **metrics,
    }


def _list_recent_message_ids(credentials: Credentials, limit: int) -> tuple[list[str], int]:
    return _paginate_message_ids(
        credentials, "/messages", {"q": "newer_than:30d"}, limit,
        lambda response: [item["id"] for item in response.get("messages", [])],
    )


def _list_history_message_ids(
    credentials: Credentials, start_history_id: str, limit: int,
) -> tuple[list[str], str, int]:
    latest_history_id = start_history_id

    def extract(response: dict) -> list[str]:
        nonlocal latest_history_id
        latest_history_id = response.get("historyId", latest_history_id)
        return [
            added["message"]["id"]
            for entry in response.get("history", [])
            for added in entry.get("messagesAdded", [])
        ]

    message_ids, pages = _paginate_message_ids(
        credentials, "/history",
        {"startHistoryId": start_history_id, "historyTypes": "messageAdded"},
        limit, extract,
    )
    return message_ids, latest_history_id, pages


def _paginate_message_ids(
    credentials: Credentials, path: str, base_params: dict, limit: int, extractor,
) -> tuple[list[str], int]:
    ids: list[str] = []
    seen: set[str] = set()
    page_token = ""
    pages = 0
    while len(ids) < limit:
        params = {**base_params, "maxResults": min(500, limit - len(ids))}
        if page_token:
            params["pageToken"] = page_token
        response = _gmail_get(credentials, path, params=params)
        pages += 1
        for message_id in extractor(response):
            if message_id not in seen:
                seen.add(message_id)
                ids.append(message_id)
                if len(ids) >= limit:
                    break
        page_token = response.get("nextPageToken", "")
        if not page_token:
            break
    if page_token:
        raise RuntimeError(
            f"Email sync reached the {limit} message safety limit; "
            "increase EMAIL_SYNC_MAX_MESSAGES"
        )
    return ids, pages


def _fetch_message(credentials: Credentials, message_id: str) -> dict:
    message = _gmail_get(credentials, f"/messages/{message_id}", params={"format": "full"})
    payload = message.get("payload", {})
    headers = {
        header["name"].lower(): header["value"] for header in payload.get("headers", [])
    }
    return {
        "message_id": message_id,
        "thread_id": message.get("threadId", ""),
        "history_id": message.get("historyId", ""),
        "headers": headers,
        "subject": _decode_header(headers.get("subject", "")),
        "sender": _decode_header(headers.get("from", "")),
        "received_at": _message_datetime(headers.get("date", ""), message.get("internalDate")),
        "body": _payload_text(payload),
    }


def _fetch_worker_count() -> int:
    raw_value = os.getenv("EMAIL_SYNC_FETCH_WORKERS", str(_DEFAULT_FETCH_WORKERS))
    try:
        configured = int(raw_value)
    except ValueError:
        logger.warning(
            "Invalid EMAIL_SYNC_FETCH_WORKERS=%r; using default %d",
            raw_value, _DEFAULT_FETCH_WORKERS,
        )
        configured = _DEFAULT_FETCH_WORKERS
    return max(1, min(configured, _MAX_FETCH_WORKERS))


def _analysis_worker_count() -> int:
    raw_value = os.getenv(
        "EMAIL_SYNC_ANALYSIS_WORKERS", str(_DEFAULT_ANALYSIS_WORKERS)
    )
    try:
        configured = int(raw_value)
    except ValueError:
        logger.warning(
            "Invalid EMAIL_SYNC_ANALYSIS_WORKERS=%r; using default %d",
            raw_value, _DEFAULT_ANALYSIS_WORKERS,
        )
        configured = _DEFAULT_ANALYSIS_WORKERS
    return max(1, min(configured, _MAX_ANALYSIS_WORKERS))


def _analyse_message(item: dict) -> tuple:
    rule_analysis = classify_application_email(
        subject=item["subject"], body=item["body"], received_at=item["received_at"],
        sender=item["sender"], headers=item["headers"],
    )
    hybrid = classify_ambiguous_email(
        rule_analysis=rule_analysis,
        subject=item["subject"], body=item["body"], received_at=item["received_at"],
        sender=item["sender"], headers=item["headers"],
    )
    analysis = hybrid.final_analysis
    body_hash = hashlib.sha256(
        item["body"].encode("utf-8", errors="ignore")
    ).hexdigest()
    return item, rule_analysis, hybrid, analysis, body_hash


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
    plain: list[str] = []
    html: list[str] = []

    def collect(part: dict) -> None:
        data = part.get("body", {}).get("data")
        if data and part.get("mimeType") == "text/plain":
            plain.append(_decode_body(data))
        elif data and part.get("mimeType") == "text/html":
            html.append(_decode_body(data))
        for child in part.get("parts", []):
            collect(child)

    collect(payload)
    if plain:
        return "\n".join(text for text in plain if text)
    return "\n".join(_html_to_text(text) for text in html if text)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return "\n".join(
        line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
    )


def _decode_body(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")
