from __future__ import annotations

import importlib
import time

import pytest

from jobradar import agent
from jobradar.llm_backend import LLMConfig
from jobradar.schemas import CVProfile, make_dedup_key


@pytest.fixture()
def streaming_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "streaming.db"))
    importlib.reload(agent.cache)
    return agent.cache


def _profile() -> CVProfile:
    return CVProfile(
        summary="Backend developer",
        skills=["Python", "SQL"],
        years_of_experience=2,
        seniority="mid",
        preferred_roles=["Backend Engineer"],
        preferred_locations=["Dublin"],
    )


def _job(index: int) -> dict:
    return {
        "title": "Backend Engineer",
        "company": f"Company {index}",
        "location": "Dublin",
        "url": f"https://example.com/jobs/{index}",
        "source": "indeed.ie" if index % 2 == 0 else "linkedin.com",
        "description_snippet": "Build Python APIs and SQL services.",
        "is_complete": True,
        "date_posted": "",
    }


def test_filtered_objects_are_persisted_then_consumed_without_db_reload(
    streaming_db,
    monkeypatch: pytest.MonkeyPatch,
):
    batches = [[_job(index)] for index in range(4)]
    producer_finished = False
    assessment_overlapped = False
    persisted_ids: list[list[int]] = []
    worker_ids: list[list[int]] = []
    emitted: list[str] = []

    def fake_stream(**kwargs):
        nonlocal producer_finished
        for batch in batches:
            time.sleep(0.02)
            yield batch
        producer_finished = True

    real_save_candidates = streaming_db.save_search_candidates

    def recording_save(run_id: str, jobs: list[dict]) -> list[str]:
        persisted_ids.append([id(job) for job in jobs])
        return real_save_candidates(run_id, jobs)

    def fake_filter(jobs: list[dict], **kwargs) -> list[dict]:
        nonlocal assessment_overlapped
        assert persisted_ids
        worker_ids.append([id(job) for job in jobs])
        if not producer_finished:
            assessment_overlapped = True
        time.sleep(0.02)
        return jobs

    def fake_flush(pf, job_all_sources, profile, llm, cv_hash, cb, on_job, language, run_id="", **kwargs):
        time.sleep(0.02)
        keys = [make_dedup_key(job["company"], job["title"]) for job, _, _ in pf.pending]
        for key in keys:
            if on_job:
                on_job(key)
        return keys, 0, len(keys)

    monkeypatch.setattr(agent, "stream_scrape_source_batches", fake_stream)
    monkeypatch.setattr(streaming_db, "save_search_candidates", recording_save)
    monkeypatch.setattr(agent, "filter_jobs_by_llm", fake_filter)
    monkeypatch.setattr(agent, "flush_assessments", fake_flush)
    monkeypatch.setattr(agent.PipelineStats, "write_report", lambda self: "benchmark.json")

    keys, stats = agent.run_search(
        profile=_profile(),
        location="Ireland",
        llm=LLMConfig(provider="gemini", model="test-model"),
        cv_hash="cv-streaming",
        on_job=emitted.append,
        force_refresh=True,
    )

    expected = [make_dedup_key(job[0]["company"], job[0]["title"]) for job in batches]
    assert keys == expected
    assert emitted == expected
    assert persisted_ids == worker_ids
    assert assessment_overlapped is True
    assert stats.overlap_elapsed > 0
    assert stats.time_to_first_job is not None
    assert stats.assessment_batches == 4
    assert stats.queue_peak >= 1
    assert {row["status"] for row in streaming_db.get_search_candidates(stats.run_id)} == {"completed"}


def test_worker_failure_propagates_and_does_not_report_success(streaming_db, monkeypatch):
    def fake_stream(**kwargs):
        yield [_job(1)]

    def failing_filter(jobs: list[dict], **kwargs) -> list[dict]:
        raise RuntimeError("assessment worker failed")

    monkeypatch.setattr(agent, "stream_scrape_source_batches", fake_stream)
    monkeypatch.setattr(agent, "filter_jobs_by_llm", failing_filter)

    with pytest.raises(RuntimeError, match="assessment worker failed"):
        agent.run_search(
            profile=_profile(),
            location="Ireland",
            llm=LLMConfig(provider="gemini", model="test-model"),
            cv_hash="cv-streaming",
            force_refresh=True,
        )
