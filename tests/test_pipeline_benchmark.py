from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jobradar.batch_scheduler import BatchScheduler, ScheduledBatch
from jobradar.pipeline_benchmark import (
    AssessmentResult,
    AssessmentTask,
    FrozenDataset,
    run_paired_benchmark,
    run_replay_benchmark,
)

FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_benchmark.json"


def test_frozen_dataset_has_stable_batches_and_hash() -> None:
    first = FrozenDataset.load(FIXTURE)
    second = FrozenDataset.load(FIXTURE)

    assert first.dataset_id == "pipeline-benchmark-fixture-v1"
    assert first.producer_finished_offset_seconds == 0.05
    assert [batch.batch_id for batch in first.batches] == [
        "indeed-ai-engineer",
        "linkedin-ai-engineer",
        "indeed-ml-engineer",
    ]
    assert first.dataset_hash == second.dataset_hash
    assert sum(len(batch.jobs) for batch in first.batches) == 5
    assert first.batches[0].metadata["source"] == "indeed.ie"
    assert first.batches[0].metadata["role"] == "AI Engineer"


def test_recorded_replay_waits_for_producer_tail_after_last_candidate_batch() -> None:
    dataset = FrozenDataset.load(FIXTURE)
    sleeps: list[float] = []
    clock_value = 0.0

    def clock() -> float:
        return clock_value

    def sleep(delay: float) -> None:
        nonlocal clock_value
        sleeps.append(delay)
        clock_value += delay

    from jobradar.pipeline_benchmark import ReplayBatchSource

    batches = list(ReplayBatchSource(dataset, clock=clock, sleep=sleep).iter_batches())

    assert len(batches) == 3
    assert clock_value == pytest.approx(0.05)
    assert sum(sleeps) == pytest.approx(0.05)


def test_replay_persists_every_candidate_before_assessment(tmp_path: Path) -> None:
    dataset = FrozenDataset.load(FIXTURE)
    checked_receipts: list[str] = []

    class ReceiptCheckingEngine:
        def assess(self, task: AssessmentTask) -> AssessmentResult:
            receipt = task.persistence_receipt
            assert receipt is not None
            assert receipt.committed_at <= time.monotonic()
            checked_receipts.append(task.batch_id)
            now = time.monotonic()
            return AssessmentResult(
                run_id=task.run_id,
                batch_id=task.batch_id,
                task_hash=task.task_hash,
                candidate_keys=receipt.candidate_keys,
                started_at=now,
                finished_at=now,
            )

    result = run_replay_benchmark(
        dataset,
        mode="streaming",
        database_path=tmp_path / "isolated.db",
        assessment_engine=ReceiptCheckingEngine(),
        timing_mode="instant",
    )

    assert checked_receipts == [batch.batch_id for batch in dataset.batches]
    assert result.persisted_candidates == 5
    assert len(result.result_keys) == 5
    assert result.metrics["processed_batches"] == 3
    for batch in dataset.batches:
        persisted = next(
            event["offset_seconds"]
            for event in result.events
            if event["event"] == "persistence_committed" and event["batch_id"] == batch.batch_id
        )
        assessment_started = next(
            event["offset_seconds"]
            for event in result.events
            if event["event"] == "assessment_started" and event["batch_id"] == batch.batch_id
        )
        assert persisted <= assessment_started


def test_paired_benchmark_changes_only_scheduling_policy(tmp_path: Path) -> None:
    dataset = FrozenDataset.load(FIXTURE)

    report = run_paired_benchmark(
        dataset,
        output_directory=tmp_path / "report",
        runs=2,
        warmups=0,
        assessment_delay_seconds=0.03,
        timing_mode="recorded",
    )

    assert report["result_equivalent"] is True
    assert report["serial"]["runs"] == 2
    assert report["streaming"]["runs"] == 2
    assert report["streaming"]["overlap_mean"] > 0
    assert report["serial"]["overlap_mean"] == 0
    assert report["streaming"]["total_mean"] < report["serial"]["total_mean"]
    assert report["streaming"]["first_result_mean"] < report["serial"]["first_result_mean"]
    assert report["paired_improvement_percent"]["total_mean"] > 0
    assert len(report["paired_improvement_percent"]["total_mean_ci95"]) == 2

    summary_path = tmp_path / "report" / "pipeline_benchmark_summary.json"
    raw_path = tmp_path / "report" / "pipeline_benchmark_runs.jsonl"
    assert json.loads(summary_path.read_text(encoding="utf-8"))["dataset_hash"] == dataset.dataset_hash
    assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 4
    assert not list(tmp_path.rglob("jobradar_cache.db"))


def test_scheduler_propagates_worker_failure() -> None:
    batch = ScheduledBatch(batch_id="batch-1", value=1, ready_at=time.monotonic(), item_count=1)

    def fail(_batch: ScheduledBatch[int]) -> None:
        raise RuntimeError("assessment failed")

    with pytest.raises(RuntimeError, match="assessment failed"):
        BatchScheduler[int]("streaming").run([batch], fail)


def test_zero_duration_pair_reports_no_percentage_instead_of_failing(tmp_path: Path) -> None:
    dataset_path = tmp_path / "empty.json"
    dataset_path.write_text(
        json.dumps({"dataset_id": "empty", "batches": [{"batch_id": "empty", "jobs": []}]}),
        encoding="utf-8",
    )

    report = run_paired_benchmark(
        FrozenDataset.load(dataset_path),
        output_directory=tmp_path / "output",
        runs=1,
        warmups=0,
        assessment_delay_seconds=0,
        timing_mode="instant",
    )

    assert report["result_equivalent"] is True
    assert "total_mean" in report["paired_improvement_percent"]
