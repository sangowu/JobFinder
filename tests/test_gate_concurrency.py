from __future__ import annotations

import threading

from jobradar import assessment, scraping
from jobradar.llm_backend import LLMConfig
from jobradar.schemas import CVProfile


def _profile() -> CVProfile:
    return CVProfile(
        summary="Python backend engineer",
        skills=["Python", "SQL"],
        years_of_experience=2,
        seniority="junior",
        preferred_roles=["Backend Engineer"],
        preferred_locations=["Dublin"],
    )


def test_jd_gate_runs_two_independent_chunks_concurrently(monkeypatch):
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_complete(**kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=1)
        with state_lock:
            active -= 1
        return assessment._BatchAssessmentResult(
            results=[
                assessment.JDAssessment(
                    relevant=True,
                    reason="match",
                    score=8,
                    strengths=[],
                    weaknesses=[],
                    matched_keywords=[],
                )
                for _ in range(assessment.BATCH_SIZE)
            ]
        )

    monkeypatch.setattr(assessment, "complete_via_tool", fake_complete)
    jobs = [(f"Backend Engineer {index}", "Build Python APIs.") for index in range(assessment.BATCH_SIZE * 2)]

    results = assessment.batch_assess_jds(
        jobs,
        _profile(),
        LLMConfig(provider="gemini", model="test-model"),
    )

    assert len(results) == len(jobs)
    assert peak == 2


def test_title_gate_runs_two_independent_chunks_concurrently(monkeypatch):
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_complete(**kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=1)
        with state_lock:
            active -= 1
        return assessment._BatchTitleAssessmentResult(
            results=[
                assessment.TitleAssessment(keep=True, reason="match")
                for _ in range(assessment.BATCH_SIZE * 2)
            ]
        )

    monkeypatch.setattr(assessment, "complete_via_tool", fake_complete)
    titles = [f"Backend Engineer {index}" for index in range(assessment.BATCH_SIZE * 4)]

    results = assessment.batch_assess_titles(
        titles,
        _profile(),
        LLMConfig(provider="gemini", model="test-model"),
    )

    assert len(results) == len(titles)
    assert peak == 2


def test_coarse_gate_runs_two_independent_chunks_concurrently(monkeypatch):
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_filter(cards, *args, **kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=1)
        with state_lock:
            active -= 1
        return scraping._default_keep_results(cards)

    monkeypatch.setattr(scraping, "_filter_card_batch_by_llm", fake_filter)
    cards = [
        {"id": index, "title": "Backend Engineer", "company": "Example", "location": "Dublin"}
        for index in range(scraping._COARSE_FILTER_BATCH_SIZE * 2)
    ]

    results = scraping._filter_cards_by_llm(
        cards,
        _profile(),
        "gemini",
        "test-model",
        target_location="Dublin",
    )

    assert len(results) == len(cards)
    assert peak == 2


def test_local_gate_stays_serial():
    assert assessment.gate_worker_count("local") == 1
    assert assessment.gate_worker_count("ollama") == 1
