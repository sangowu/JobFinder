import time
from datetime import datetime, timezone

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
    assert "# HELP jobradar_email_sync_auth_failures_total" in output


def test_record_sync_result_updates_prometheus_metrics(monkeypatch):
    labels = {"trigger": "manual", "status": "success"}

    runs_before = _sample_value(
        EMAIL_SYNC_RUNS,
        "jobradar_email_sync_runs_total",
        labels,
    )
    duration_count_before = _sample_value(
        EMAIL_SYNC_DURATION_SECONDS,
        "jobradar_email_sync_duration_seconds_count",
        labels,
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
        labels,
    ) == runs_before + 1

    assert _sample_value(
        EMAIL_SYNC_DURATION_SECONDS,
        "jobradar_email_sync_duration_seconds_count",
        labels,
    ) == duration_count_before + 1

    last_success = _sample_value(
        EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS,
        "jobradar_email_sync_last_success_timestamp_seconds",
        {},
    )
    assert before_wall_time <= last_success <= time.time()