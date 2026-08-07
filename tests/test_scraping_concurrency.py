from __future__ import annotations

import threading

from jobradar import scraping


def _job(url: str, source: str) -> dict:
    return {
        "title": url,
        "company": "Example",
        "location": "Dublin",
        "url": url,
        "apply_url": url,
        "source": source,
        "is_complete": True,
        "description_snippet": "Python",
        "date_posted": "",
        "is_remote": False,
    }


def test_indeed_roles_use_at_most_two_workers_and_deduplicate(monkeypatch):
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_scrape(role, limit, country, hours_old):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        if role in {"AI Engineer", "ML Engineer"}:
            barrier.wait(timeout=1)
        with state_lock:
            active -= 1
        shared_url = "https://example.com/shared"
        return [_job(shared_url if role != "Data Engineer" else "https://example.com/data", "indeed.ie")]

    monkeypatch.setattr(scraping, "scrape_indeed_jobspy", fake_scrape)
    monkeypatch.setattr(scraping.random, "uniform", lambda *_: 0)

    jobs = scraping.scrape_indeed_jobspy_multi(
        ["AI Engineer", "ML Engineer", "Data Engineer"],
        limit_per_role=20,
    )

    assert peak == 2
    assert [job["url"] for job in jobs] == [
        "https://example.com/shared",
        "https://example.com/data",
    ]


def test_indeed_role_starts_share_one_rate_limit(monkeypatch):
    sleep_calls: list[float] = []
    clock = iter([0.0, 0.0, 0.0, 2.0, 2.0, 4.0])

    monkeypatch.setattr(scraping.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scraping.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(scraping.random, "uniform", lambda *_: 2.0)
    monkeypatch.setattr(
        scraping,
        "scrape_indeed_jobspy",
        lambda role, limit, country, hours_old: [_job(f"https://example.com/{role}", "indeed.ie")],
    )

    jobs = scraping.scrape_indeed_jobspy_multi(["one", "two", "three"])

    assert len(jobs) == 3
    assert sleep_calls == [2.0, 2.0]


def test_linkedin_roles_use_at_most_two_workers_and_deduplicate(monkeypatch):
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_scrape(role, limit, location, hours_old):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        if role in {"AI Engineer", "ML Engineer"}:
            barrier.wait(timeout=1)
        with state_lock:
            active -= 1
        shared_url = "https://example.com/shared"
        return [_job(shared_url if role != "Data Engineer" else "https://example.com/data", "linkedin.com")]

    monkeypatch.setattr(scraping, "scrape_linkedin_jobspy", fake_scrape)
    monkeypatch.setattr(scraping.random, "uniform", lambda *_: 0)

    jobs = scraping.scrape_linkedin_jobspy_multi(
        ["AI Engineer", "ML Engineer", "Data Engineer"],
        limit_per_role=20,
    )

    assert peak == 2
    assert [job["url"] for job in jobs] == [
        "https://example.com/shared",
        "https://example.com/data",
    ]


def test_linkedin_role_starts_share_one_rate_limit(monkeypatch):
    sleep_calls: list[float] = []
    clock = iter([0.0, 0.0, 0.0, 3.0, 3.0, 6.0])

    monkeypatch.setattr(scraping.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scraping.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(scraping.random, "uniform", lambda *_: 3.0)
    monkeypatch.setattr(
        scraping,
        "scrape_linkedin_jobspy",
        lambda role, limit, location, hours_old: [_job(f"https://example.com/{role}", "linkedin.com")],
    )

    jobs = scraping.scrape_linkedin_jobspy_multi(["one", "two", "three"])

    assert len(jobs) == 3
    assert sleep_calls == [3.0, 3.0]


def test_indeed_and_linkedin_sources_run_concurrently(monkeypatch):
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_source(source: str):
        def run(**kwargs):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=1)
            with state_lock:
                active -= 1
            return [_job(f"https://example.com/{source}", source)]

        return run

    monkeypatch.setattr(scraping, "scrape_indeed_jobspy_multi", fake_source("indeed.ie"))
    monkeypatch.setattr(scraping, "scrape_linkedin_jobspy_multi", fake_source("linkedin.com"))

    jobs = scraping.scrape_sources(
        roles=["AI Engineer"],
        location="Ireland",
        hours_old=None,
    )

    assert peak == 2
    assert {job["source"] for job in jobs} == {"indeed.ie", "linkedin.com"}


def test_streaming_source_yields_batch_before_scrape_finishes(monkeypatch):
    release_sources = threading.Event()
    source_finished = threading.Event()

    def fake_indeed(**kwargs):
        batch = [_job("https://example.com/indeed", "indeed.ie")]
        kwargs["on_batch_timed"]("AI Engineer", batch, 10.0, 12.5)
        release_sources.wait(timeout=1)
        source_finished.set()
        return batch

    def fake_linkedin(**kwargs):
        release_sources.wait(timeout=1)
        return []

    monkeypatch.setattr(scraping, "scrape_indeed_jobspy_multi", fake_indeed)
    monkeypatch.setattr(scraping, "scrape_linkedin_jobspy_multi", fake_linkedin)

    events = scraping.stream_scrape_source_batch_events(
        roles=["AI Engineer"],
        location="Ireland",
        hours_old=None,
    )
    try:
        first = next(events)
        assert first.source == "indeed.ie"
        assert first.role == "AI Engineer"
        assert first.scrape_elapsed == 2.5
        assert first.raw_count == 1
        assert first.unique_count == 1
        assert [job["url"] for job in first.jobs] == ["https://example.com/indeed"]
        assert not source_finished.is_set()
    finally:
        release_sources.set()
        list(events)


def test_streaming_source_wrapper_preserves_list_interface(monkeypatch):
    def fake_indeed(**kwargs):
        batch = [_job("https://example.com/indeed", "indeed.ie")]
        kwargs["on_batch_timed"]("AI Engineer", batch, 10.0, 11.0)
        return batch

    monkeypatch.setattr(scraping, "scrape_indeed_jobspy_multi", fake_indeed)
    monkeypatch.setattr(scraping, "scrape_linkedin_jobspy_multi", lambda **kwargs: [])

    batches = list(scraping.stream_scrape_source_batches(["AI Engineer"], "Ireland", hours_old=None))

    assert len(batches) == 1
    assert isinstance(batches[0], list)
    assert batches[0][0]["url"] == "https://example.com/indeed"
