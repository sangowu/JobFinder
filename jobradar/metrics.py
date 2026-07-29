from prometheus_client import Counter, Gauge, Histogram

_EMAIL_SYNC_DURATION_BUCKETS = (
    0.1,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)

EMAIL_SYNC_RUNS = Counter(
    "jobradar_email_sync_runs",
    "Total Gmail synchronization runs.",
    ("trigger", "status"),
)

EMAIL_SYNC_DURATION_SECONDS = Histogram(
    "jobradar_email_sync_duration_seconds",
    "Duration of Gmail synchronization runs in seconds.",
    ("trigger", "status"),
    buckets=_EMAIL_SYNC_DURATION_BUCKETS,
)

EMAIL_SYNC_LAST_SUCCESS_TIMESTAMP_SECONDS = Gauge(
    "jobradar_email_sync_last_success_timestamp_seconds",
    "Unix timestamp of the last successful Gmail synchronization.",
)

EMAIL_SYNC_AUTH_FAILURES = Counter(
    "jobradar_email_sync_auth_failures",
    "Total Gmail OAuth authentication failures.",
    ("trigger",),
)

