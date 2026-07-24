from __future__ import annotations

import importlib
from datetime import datetime
from unittest.mock import patch

import pytest

from jobradar.email_llm_classifier import (
    EmailLLMDecision,
    classify_ambiguous_email,
    llm_trigger_reason,
)
from jobradar.schemas import ApplicationEmailAnalysis


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "applications.db"))
    import jobradar.application_store as store_mod

    importlib.reload(store_mod)
    return store_mod


def _analysis(**updates) -> ApplicationEmailAnalysis:
    values = {
        "is_job_related": True,
        "status": "unknown",
        "company": "",
        "job_title": "",
        "event_at": datetime(2026, 7, 22),
        "confidence": 0.55,
        "summary": "Application update",
        "classification_reason": "job_signal",
        "classifier_version": "rules-v2",
    }
    values.update(updates)
    return ApplicationEmailAnalysis(**values)


def test_subscription_does_not_trigger_llm():
    rule = _analysis(
        is_job_related=False,
        classification_reason="subscription:content:job alert",
        confidence=0.05,
    )

    assert llm_trigger_reason(rule) == ""


def test_bulk_header_without_subscription_content_triggers_llm():
    rule = _analysis(
        is_job_related=False,
        classification_reason="bulk_header_uncertain:list-unsubscribe",
        confidence=0.05,
    )

    assert llm_trigger_reason(rule) == "bulk_header_uncertain"


def test_high_confidence_llm_result_is_accepted(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "gemini")
    monkeypatch.setenv("DEFAULT_MODEL", "gemini-test")
    decision = EmailLLMDecision(
        is_job_related=True,
        status="interview",
        company="Acme",
        job_title="AI Engineer",
        confidence=0.93,
        summary="Interview invitation",
        reasoning="Specific candidacy update",
    )

    with patch("jobradar.email_llm_classifier.complete_structured", return_value=decision):
        result = classify_ambiguous_email(
            rule_analysis=_analysis(), subject="Application update",
            body="We would like to invite you to interview.", sender="Acme",
            received_at=datetime(2026, 7, 22),
        )

    assert result.decision == "llm_auto"
    assert result.final_analysis.status == "interview"
    assert result.final_analysis.company == "Acme"


def test_medium_confidence_llm_result_goes_to_pending():
    decision = EmailLLMDecision(
        is_job_related=True,
        status="assessment",
        company="Acme",
        job_title="AI Engineer",
        confidence=0.7,
        summary="Possible assessment",
        reasoning="Wording is ambiguous",
    )

    with patch("jobradar.email_llm_classifier.complete_structured", return_value=decision):
        result = classify_ambiguous_email(
            rule_analysis=_analysis(), subject="Next step", body="Please complete this task.",
            sender="Acme", received_at=datetime(2026, 7, 22),
        )

    assert result.decision == "llm_pending"
    assert result.final_analysis.is_job_related is True
    assert result.final_analysis.status == "unknown"


def test_llm_failure_falls_back_to_rule_result():
    rule = _analysis()
    with patch(
        "jobradar.email_llm_classifier.complete_structured",
        side_effect=RuntimeError("provider unavailable"),
    ):
        result = classify_ambiguous_email(
            rule_analysis=rule, subject="Update", body="Application update",
            sender="ATS", received_at=datetime(2026, 7, 22),
        )

    assert result.decision == "rules_fallback_error"
    assert result.final_analysis == rule
    assert "provider unavailable" in result.error


def test_local_observations_compute_human_evaluation_metrics(store):
    rule = _analysis()
    final = _analysis(classification_reason="llm_pending:ambiguous_status")
    llm_prediction = _analysis(
        status="interview", confidence=0.7, classification_reason="llm:ambiguous_status"
    )
    store.record_classification_observation(
        provider="gmail", message_id="msg-1", application_id=7, body_hash="hash-only",
        trigger_reason="ambiguous_status", decision="llm_pending", llm_provider="gemini",
        llm_model="gemini-test", latency_ms=120, input_tokens=100, output_tokens=20,
        disagreement=True, error_message="", rule_analysis=rule, llm_analysis=llm_prediction,
        final_analysis=final,
    )
    store.record_classification_feedback(
        7, is_job_related=True, status="interview", company="Acme",
        job_title="AI Engineer", action="confirmed",
    )

    metrics = store.get_classification_metrics()

    assert metrics["total_classified"] == 1
    assert metrics["llm_calls"] == 1
    assert metrics["llm_pending"] == 1
    assert metrics["input_tokens"] == 100
    assert metrics["reviewed"] == 1
    assert metrics["related_accuracy"] == 1.0
    assert metrics["status_accuracy"] == 1.0
    assert metrics["confusion"] == {"interview": {"interview": 1}}
