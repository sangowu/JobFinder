import time
from datetime import datetime, timezone

import requests
from prometheus_client import generate_latest

from jobradar import email_sync
from jobradar.metrics import (
    EMAIL_SYNC_DURATION_SECONDS,
    EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS,
    EMAIL_SYNC_RUNS,
)


def _sample_value(metric, sample_name: str, labels: dict[str, str]) -> float:
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == sample_name and sample.labels == labels:
                return sample.value
    return 0.0

def test_email_sync_metrics_are_registered():
    output = generate_latest().decode("utf-8")

    assert "# HELP jobradar_email_sync_runs_total" in output
    assert "# HELP jobradar_email_sync_duration_seconds" in output
    assert "# HELP jobradar_email_sync_last_success_timestamp_seconds" in output


def test_record_sync_result_updates_prometheus_metrics(monkeypatch):
    run_labels = {"trigger": "manual", "status": "success", "reason": "none"}
    duration_labels = {"trigger": "manual", "status": "success"}

    runs_before = _sample_value(
        EMAIL_SYNC_RUNS,
        "jobradar_email_sync_runs_total",
        run_labels,
    )
    duration_count_before = _sample_value(
        EMAIL_SYNC_DURATION_SECONDS,
        "jobradar_email_sync_duration_seconds_count",
        duration_labels,
    )
    before_wall_time = time.time()

    monkeypatch.setattr(
        email_sync.application_store,
        "record_sync_run",
        lambda **kwargs: {"id": 123},
    )

    result = email_sync._record_sync_result(
        trigger="manual",
        status="success",
        started_at=datetime.now(timezone.utc),
        started_perf=time.perf_counter() - 0.01,
        metrics={
            "candidates": 0,
            "already_processed": 0,
            "scanned": 0,
            "matched": 0,
            "pages": 0,
            "job_related": 0,
            "subscription_filtered": 0,
            "unrelated": 0,
            "unknown": 0,
            "failed_messages": 0,
        },
        message="Email sync complete",
        sync_mode="full",
    )

    assert result["ok"] is True

    assert _sample_value(
        EMAIL_SYNC_RUNS,
        "jobradar_email_sync_runs_total",
        run_labels,
    ) == runs_before + 1

    assert _sample_value(
        EMAIL_SYNC_DURATION_SECONDS,
        "jobradar_email_sync_duration_seconds_count",
        duration_labels,
    ) == duration_count_before + 1

    last_success = _sample_value(
        EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS,
        "jobradar_email_sync_last_success_timestamp_seconds",
        {},
    )
    assert before_wall_time <= last_success <= time.time()


def _http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(response=response)


def test_failure_reason_classification():
    # 授权失效 → auth
    assert email_sync._failure_reason(email_sync.EmailAuthError("token expired")) == "auth"

    # 限流和网络抖动 → rate_limit
    assert email_sync._failure_reason(_http_error(429)) == "rate_limit"
    assert email_sync._failure_reason(requests.Timeout()) == "rate_limit"
    assert email_sync._failure_reason(requests.ConnectionError()) == "rate_limit"

    # 其他一切 → other
    assert email_sync._failure_reason(_http_error(500)) == "other"
    assert email_sync._failure_reason(ValueError("boom")) == "other"

    # response 为 None（连接层就失败，压根没拿到 HTTP 响应）
    assert email_sync._failure_reason(requests.HTTPError()) == "other"


def test_record_sync_result_tags_auth_failures(monkeypatch):
    labels = {"trigger": "scheduled", "status": "failed", "reason": "auth"}

    runs_before = _sample_value(
        EMAIL_SYNC_RUNS,
        "jobradar_email_sync_runs_total",
        labels,
    )
    last_success_before = _sample_value(
        EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS,
        "jobradar_email_sync_last_success_timestamp_seconds",
        {},
    )

    monkeypatch.setattr(
        email_sync.application_store,
        "record_sync_run",
        lambda **kwargs: {"id": 456},
    )

    result = email_sync._record_sync_result(
        trigger="scheduled",
        status="failed",
        reason="auth",
        started_at=datetime.now(timezone.utc),
        started_perf=time.perf_counter() - 0.01,
        metrics={
            "candidates": 0,
            "already_processed": 0,
            "scanned": 0,
            "matched": 0,
            "pages": 0,
            "job_related": 0,
            "subscription_filtered": 0,
            "unrelated": 0,
            "unknown": 0,
            "failed_messages": 0,
        },
        message="Google authorization is invalid; reconnect the account",
        sync_mode="full",
    )

    assert result["ok"] is False

    assert _sample_value(
        EMAIL_SYNC_RUNS,
        "jobradar_email_sync_runs_total",
        labels,
    ) == runs_before + 1

    assert _sample_value(
        EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS,
        "jobradar_email_sync_last_success_timestamp_seconds",
        {},
    ) == last_success_before

