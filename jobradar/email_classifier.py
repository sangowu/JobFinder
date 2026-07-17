"""Conservative local classification for job-application email messages."""
from __future__ import annotations

import re
from datetime import datetime

from jobradar.schemas import ApplicationEmailAnalysis

_STATUS_PATTERNS = {
    "offer": (r"\b(job offer|offer letter|pleased to offer)\b",),
    "interview": (r"\b(interview|schedule (?:a|your) call|meet the hiring)\b",),
    "assessment": (r"\b(assessment|coding challenge|online test|take.home)\b",),
    "rejected": (r"\b(unfortunately|not moving forward|other candidates|not selected|unsuccessful)\b",),
    "withdrawn": (r"\b(application withdrawn|withdrawal confirmed)\b",),
    "submitted": (r"\b(application (?:has been )?(?:received|submitted)|thank you for applying)\b",),
}
_SUBSCRIPTION_SIGNAL = re.compile(
    r"\b("
    r"job alerts?|jobs? matching your|new jobs? (?:for|in)|recommended jobs?|"
    r"jobs? you may (?:like|be interested in)|daily job digest|weekly job digest|"
    r"latest (?:jobs?|vacancies)|career opportunities|talent community (?:news|update)"
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


def classify_application_email(
    *,
    subject: str,
    body: str,
    received_at: datetime,
    sender: str = "",
    headers: dict[str, str] | None = None,
) -> ApplicationEmailAnalysis:
    text = f"{subject}\n{body[:12000]}"
    status = "unknown"
    for candidate, patterns in _STATUS_PATTERNS.items():
        if any(re.search(pattern, text, re.I) for pattern in patterns):
            status = candidate
            break
    header_values = {key.lower(): value for key, value in (headers or {}).items()}
    is_bulk = bool(
        header_values.get("list-unsubscribe")
        or header_values.get("list-id")
        or header_values.get("precedence", "").lower() in {"bulk", "list"}
    )
    is_subscription = status == "unknown" and (
        bool(_SUBSCRIPTION_SIGNAL.search(text)) or is_bulk
    )
    is_related = not is_subscription and (
        status != "unknown" or bool(_JOB_SIGNAL.search(text))
    )
    company = _extract(text, _COMPANY_PATTERNS)
    title = _extract(text, _TITLE_PATTERNS)
    confidence = 0.9 if status != "unknown" else (0.55 if is_related else 0.05)
    return ApplicationEmailAnalysis(
        is_job_related=is_related,
        status=status,
        company=company,
        job_title=title,
        event_at=received_at,
        confidence=confidence,
        summary=subject.strip()[:240],
    )


def _extract(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" .,-")
    return ""