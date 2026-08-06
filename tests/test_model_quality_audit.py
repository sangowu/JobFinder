from __future__ import annotations

import csv
import json

from google.genai import types

from jobradar.llm_backend import _gemini_thinking_config
from jobradar.model_quality_audit import (
    build_quality_report,
    build_run_decisions,
    score_blind_reviews,
    write_blind_review_packages,
)


def _run(model: str, passed: list[str], decisions: list[dict], elapsed: float) -> dict:
    return {
        "dataset_hash": "dataset",
        "profile_hash": "profile",
        "provider": "gemini",
        "model": model,
        "total_elapsed": elapsed,
        "time_to_first_job": elapsed / 2,
        "result_keys": passed,
        "llm_calls": 4,
        "tokens_in": 100,
        "tokens_out": 20,
        "pipeline_stats": {"llm_assessed": 2, "evaluation_tasks": 1},
        "audit_decisions": decisions,
    }


def test_flash_lite_thinking_is_explicitly_minimal() -> None:
    config = _gemini_thinking_config(types, "gemini-3.5-flash-lite")
    assert config is not None
    assert str(config.thinking_level.value).lower() == "minimal"
    assert _gemini_thinking_config(types, "gemini-3.1-flash-lite") is not None
    assert _gemini_thinking_config(types, "gemini-3.1-pro-preview") is None


def test_build_run_decisions_uses_terminal_events_and_visible_keys() -> None:
    jobs = [
        {"title": "AI Engineer", "company": "A", "url": "https://a"},
        {"title": "Sales Manager", "company": "B", "url": "https://b"},
    ]
    decisions = build_run_decisions(
        jobs,
        result_keys=["a|ai engineer"],
        filter_events=[
            {
                "stage": "title_relevance",
                "title": "Sales Manager",
                "company": "B",
                "reason": "unrelated",
                "details": {},
            }
        ],
        cached_jobs={},
    )
    assert decisions[0]["decision"] == "pass"
    assert decisions[1]["decision"] == "reject"
    assert decisions[1]["terminal_stage"] == "title_relevance"


def test_quality_report_and_blind_packages(tmp_path) -> None:
    reference_decisions = [
        {"dedup_key": "a|ai engineer", "title": "AI Engineer", "company": "A", "decision": "pass", "terminal_stage": "final_visible"},
        {"dedup_key": "b|sales", "title": "Sales", "company": "B", "decision": "reject", "terminal_stage": "title_relevance"},
    ]
    replacement_decisions = [
        {"dedup_key": "a|ai engineer", "title": "AI Engineer", "company": "A", "decision": "reject", "terminal_stage": "jd_assessment"},
        {"dedup_key": "b|sales", "title": "Sales", "company": "B", "decision": "reject", "terminal_stage": "title_relevance"},
    ]
    report = build_quality_report(
        {
            "reference": [_run("model-a", ["a|ai engineer"], reference_decisions, 10)],
            "replacement": [_run("model-b", [], replacement_decisions, 8)],
        },
        {"reference": "model-a", "replacement": "model-b"},
    )
    assert report["ground_truth_status"] == "pending_two_human_blind_reviews"
    assert report["disagreements"][0]["dedup_key"] == "a|ai engineer"

    dataset = {
        "batches": [
            {
                "jobs": [
                    {"title": "AI Engineer", "company": "A", "location": "Dublin", "description_snippet": "Build AI"},
                    {"title": "Sales", "company": "B", "location": "Dublin", "description_snippet": "Sell"},
                ]
            }
        ]
    }
    reviewer_a, reviewer_b, manifest = write_blind_review_packages(dataset, tmp_path)
    assert len(list(csv.DictReader(reviewer_a.open(encoding="utf-8-sig")))) == 2
    assert len(list(csv.DictReader(reviewer_b.open(encoding="utf-8-sig")))) == 2
    assert len(json.loads(manifest.read_text(encoding="utf-8"))) == 2
    assert (tmp_path / "README.md").exists()


def test_score_blind_reviews_requires_complete_labels() -> None:
    report = {
        "models": {"reference": "model-a", "replacement": "model-b"},
        "decision_stability": {"reference": 0.9, "replacement": 0.9},
        "performance_change_percent": {"mean_total": 20},
        "acceptance_gates": {
            "recommended_recall_delta_min_percentage_points": -3,
            "stretch_false_reject_delta_max_percentage_points": 3,
            "false_pass_rate_must_not_increase": True,
            "decision_stability_must_not_decrease": True,
            "mean_latency_improvement_min_percent": 15,
        },
        "job_frequencies": {
            "reference": {"a|ai": {"pass_count": 5}, "b|sales": {"pass_count": 0}},
            "replacement": {"a|ai": {"pass_count": 4}, "b|sales": {"pass_count": 1}},
        }
    }
    manifest = {"J001": "a|ai", "J002": "b|sales"}
    reviewer_a = [
        {"blind_id": "J001", "human_decision": "recommend"},
        {"blind_id": "J002", "human_decision": "reject"},
    ]
    reviewer_b = [
        {"blind_id": "J001", "human_decision": "recommend"},
        {"blind_id": "J002", "human_decision": "stretch"},
    ]
    score = score_blind_reviews(report, manifest, reviewer_a, reviewer_b)
    assert score["reviewer_agreement_rate"] == 0.5
    assert score["status"] == "needs_adjudication"
    assert score["metrics_on_agreed_labels"]["replacement"]["recommend_recall"] == 1

    adjudicated = score_blind_reviews(
        report,
        manifest,
        reviewer_a,
        reviewer_b,
        [{"blind_id": "J002", "human_decision": "reject"}],
    )
    assert adjudicated["status"] == "complete"
    assert adjudicated["unresolved_jobs"] == []
