"""Conservative local classification for job-application email messages."""
from __future__ import annotations

import re
from datetime import datetime

from jobradar.schemas import ApplicationEmailAnalysis

CLASSIFIER_VERSION = "rules-v3"

_STATUS_PATTERNS = {
    "offer": (r"\b(job offer|offer letter|pleased to offer)\b",),
    "interview": (r"\b(interview|schedule (?:a|your) call|meet the hiring)\b",),
    "assessment": (r"\b(assessment|coding challenge|online test|take.home)\b",),
    "rejected": (r"\b(unfortunately|not moving forward|other candidates|not selected|unsuccessful)\b",),
    "withdrawn": (r"\b(application withdrawn|withdrawal confirmed)\b",),
    "submitted": (
        r"\b("
        r"application (?:has been )?(?:received|submitted)|"
        r"thank you for applying|thank you for your application|"
        r"we(?:'ve| have) received your application|application confirmation"
        r")\b",
    ),
}
_SUBSCRIPTION_SIGNAL = re.compile(
    r"\b("
    r"job alerts?|jobs? matching your|new jobs? (?:for|in)|recommended jobs?|"
    r"jobs? you may (?:like|be interested in)|daily job digest|weekly job digest|"
    r"latest (?:jobs?|vacancies)|career opportunities|talent community (?:news|update)|"
    r"share their thoughts on linkedin|better work|newsletter"
    r")\b",
    re.I,
)
_JOB_SIGNAL = re.compile(r"\b(application|applying|position|role|vacancy|candidate|recruitment)\b", re.I)
_COMPANY_PATTERNS = (
    re.compile(r"(?:at|with)\s+([A-Z][\w&.' -]{1,60})(?:[,.\n]| for )"),
    re.compile(r"(?:company|organisation|organization)\s*:\s*([^\n]{2,60})", re.I),
)
_TITLE_PATTERNS = (
    re.compile(r"(?:position|role|job title)\s*:\s*([^\n]{2,100})", re.I),
    re.compile(r"application (?:for|to) (?:the )?([^\n,.]{2,100})", re.I),
)
_REFERENCE_PATTERNS = (
    re.compile(
        r"(?:application|candidate|requisition|job)\s*(?:id|reference|ref|number|no\.?|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]{2,40})",
        re.I,
    ),
)


def classify_application_email(
    *,
    subject: str,
    body: str,
    received_at: datetime,
    sender: str = "",
    headers: dict[str, str] | None = None,
) -> ApplicationEmailAnalysis:
    text = f"{subject}\n{body[:12000]}"
    subject_status = _match_status(subject)
    subject_subscription = _SUBSCRIPTION_SIGNAL.search(subject)
    status = subject_status or ("unknown" if subject_subscription else _match_status(text))
    header_values = {key.lower(): value for key, value in (headers or {}).items()}
    bulk_header = next((
        name for name in ("list-unsubscribe", "list-id") if header_values.get(name)
    ), "")
    is_bulk = bool(
        bulk_header
        or header_values.get("precedence", "").lower() in {"bulk", "list"}
    )
    subscription_match = subject_subscription or _SUBSCRIPTION_SIGNAL.search(text)
    is_subscription = status == "unknown" and bool(subscription_match)
    is_related = not is_subscription and (
        status != "unknown" or bool(_JOB_SIGNAL.search(text))
    )
    company = _extract(text, _COMPANY_PATTERNS)
    title = _extract(text, _TITLE_PATTERNS)
    reference = _extract(text, _REFERENCE_PATTERNS)
    confidence = 0.9 if status != "unknown" else (0.55 if is_related else 0.05)
    if status != "unknown":
        reason = f"transactional:{status}"
    elif subscription_match:
        reason = f"subscription:content:{subscription_match.group(0).lower()}"
    elif is_bulk:
        reason = f"bulk_header_uncertain:{bulk_header or 'precedence'}"
    elif is_related:
        reason = "job_signal"
    else:
        reason = "unrelated"
    return ApplicationEmailAnalysis(
        is_job_related=is_related,
        status=status,
        company=company,
        job_title=title,
        application_reference=reference or None,
        event_at=received_at,
        confidence=confidence,
        summary=subject.strip()[:240],
        classification_reason=reason,
        classifier_version=CLASSIFIER_VERSION,
    )


def _match_status(text: str) -> str:
    for candidate, patterns in _STATUS_PATTERNS.items():
        if any(re.search(pattern, text, re.I) for pattern in patterns):
            return candidate
    return "unknown"


def _extract(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" .,-")
    return ""
