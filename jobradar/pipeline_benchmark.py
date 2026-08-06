"""Replay-driven serial/streaming comparison for persisted assessment batches.

Datasets contain post-filter candidate batches and their relative availability
times. The benchmark writes only to a caller-provided SQLite database and never
imports the production cache or SSE layer.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import sqlite3
import statistics
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from jobradar.batch_scheduler import BatchScheduler, ScheduledBatch, SchedulerMetrics

TimingMode = Literal["instant", "recorded"]


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_key(job: dict) -> str:
    explicit = str(job.get("dedup_key") or "").strip()
    if explicit:
        return explicit
    identity = {
        "company": str(job.get("company") or "").strip().casefold(),
        "title": str(job.get("title") or "").strip().casefold(),
        "url": str(job.get("url") or "").strip(),
    }
    return canonical_hash(identity)


@dataclass(frozen=True)
class FrozenBatch:
    batch_id: str
    ready_offset_seconds: float
    jobs: tuple[dict, ...]
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict, index: int) -> FrozenBatch:
        batch_id = str(value.get("batch_id") or f"batch-{index + 1:04d}")
        offset = float(value.get("ready_offset_seconds", value.get("ready_offset_ms", 0) / 1000))
        jobs = value.get("jobs")
        if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
            raise ValueError(f"{batch_id}: jobs must be a list of objects")
        if offset < 0:
            raise ValueError(f"{batch_id}: ready offset must be non-negative")
        metadata = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in {"batch_id", "ready_offset_seconds", "ready_offset_ms", "jobs"}
        }
        return cls(
            batch_id=batch_id,
            ready_offset_seconds=offset,
            jobs=tuple(copy.deepcopy(jobs)),
            metadata=metadata,
        )


@dataclass(frozen=True)
class FrozenDataset:
    dataset_id: str
    batches: tuple[FrozenBatch, ...]
    dataset_hash: str
    producer_finished_offset_seconds: float
    metadata: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> FrozenDataset:
        dataset_path = Path(path)
        if dataset_path.suffix.lower() == ".jsonl":
            values = [
                json.loads(line)
                for line in dataset_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            dataset_id = dataset_path.stem
            dataset_metadata: dict = {}
        else:
            raw = json.loads(dataset_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                values = raw
                dataset_id = dataset_path.stem
                dataset_metadata = {}
            elif isinstance(raw, dict):
                values = raw.get("batches")
                dataset_id = str(raw.get("dataset_id") or dataset_path.stem)
                dataset_metadata = {
                    key: copy.deepcopy(item)
                    for key, item in raw.items()
                    if key not in {"dataset_id", "batches"}
                }
            else:
                raise ValueError("Dataset must be a JSON object, JSON array, or JSONL batches")
        if not isinstance(values, list) or not values:
            raise ValueError("Dataset must contain at least one batch")

        batches = tuple(FrozenBatch.from_dict(value, index) for index, value in enumerate(values))
        batch_ids = [batch.batch_id for batch in batches]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("Dataset batch_id values must be unique")
        offsets = [batch.ready_offset_seconds for batch in batches]
        if offsets != sorted(offsets):
            raise ValueError("Dataset batches must be ordered by ready offset")
        producer_finished_offset = float(
            dataset_metadata.get("producer_finished_offset_seconds", max(offsets, default=0.0))
        )
        if producer_finished_offset < max(offsets, default=0.0):
            raise ValueError("producer finished offset cannot precede the last batch")
        candidate_keys = [_candidate_key(job) for batch in batches for job in batch.jobs]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("Frozen post-filter candidates must be unique across batches")
        normalized_batches = [
            {
                "batch_id": batch.batch_id,
                "ready_offset_seconds": batch.ready_offset_seconds,
                **batch.metadata,
                "jobs": batch.jobs,
            }
            for batch in batches
        ]
        normalized = {
            "dataset_id": dataset_id,
            **dataset_metadata,
            "producer_finished_offset_seconds": producer_finished_offset,
            "batches": normalized_batches,
        }
        return cls(
            dataset_id=dataset_id,
            batches=batches,
            dataset_hash=canonical_hash(normalized),
            producer_finished_offset_seconds=producer_finished_offset,
            metadata=dataset_metadata,
        )


class ReplayBatchSource:
    """Yield frozen batches immediately or according to their recorded offsets."""

    def __init__(
        self,
        dataset: FrozenDataset,
        *,
        timing_mode: TimingMode = "recorded",
        speed_factor: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timing_mode not in ("instant", "recorded"):
            raise ValueError(f"Unsupported timing mode: {timing_mode}")
        if speed_factor <= 0:
            raise ValueError("speed_factor must be greater than zero")
        self.dataset = dataset
        self.timing_mode = timing_mode
        self.speed_factor = speed_factor
        self._clock = clock
        self._sleep = sleep

    def iter_batches(self) -> Iterator[FrozenBatch]:
        started_at = self._clock()
        for batch in self.dataset.batches:
            if self.timing_mode == "recorded":
                target = started_at + batch.ready_offset_seconds / self.speed_factor
                remaining = target - self._clock()
                if remaining > 0:
                    self._sleep(remaining)
            yield FrozenBatch(
                batch_id=batch.batch_id,
                ready_offset_seconds=batch.ready_offset_seconds,
                jobs=tuple(copy.deepcopy(batch.jobs)),
                metadata=copy.deepcopy(batch.metadata),
            )
        if self.timing_mode == "recorded":
            target = started_at + self.dataset.producer_finished_offset_seconds / self.speed_factor
            remaining = target - self._clock()
            if remaining > 0:
                self._sleep(remaining)


@dataclass(frozen=True)
class PersistenceReceipt:
    run_id: str
    batch_id: str
    committed_at: float
    candidate_keys: tuple[str, ...]


@dataclass(frozen=True)
class AssessmentTask:
    run_id: str
    batch_id: str
    candidates: tuple[dict, ...]
    task_hash: str
    persistence_receipt: PersistenceReceipt | None = None


@dataclass(frozen=True)
class AssessmentResult:
    run_id: str
    batch_id: str
    task_hash: str
    candidate_keys: tuple[str, ...]
    started_at: float
    finished_at: float
    tokens_in: int = 0
    tokens_out: int = 0


class AssessmentEngine(Protocol):
    def assess(self, task: AssessmentTask) -> AssessmentResult:
        ...


class DelayAssessmentEngine:
    """Deterministic assessment adapter used to isolate scheduling behavior."""

    def __init__(
        self,
        delay_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        self.delay_seconds = delay_seconds
        self._clock = clock
        self._sleep = sleep

    def assess(self, task: AssessmentTask) -> AssessmentResult:
        receipt = task.persistence_receipt
        if receipt is None:
            raise RuntimeError(f"Assessment task {task.batch_id} was not persisted")
        started_at = self._clock()
        if started_at < receipt.committed_at:
            raise RuntimeError(f"Assessment task {task.batch_id} started before persistence commit")
        if self.delay_seconds:
            self._sleep(self.delay_seconds)
        return AssessmentResult(
            run_id=task.run_id,
            batch_id=task.batch_id,
            task_hash=task.task_hash,
            candidate_keys=receipt.candidate_keys,
            started_at=started_at,
            finished_at=self._clock(),
        )


class IsolatedCandidateRepository:
    """Minimal SQLite adapter used only by replay benchmarks."""

    def __init__(self, database_path: str | Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.database_path = Path(database_path)
        self._clock = clock
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            with con:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS benchmark_candidates (
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        candidate_key TEXT NOT NULL,
                        candidate_json TEXT NOT NULL,
                        PRIMARY KEY (run_id, batch_id, candidate_key)
                    )
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def persist(self, task: AssessmentTask) -> PersistenceReceipt:
        rows = [
            (
                task.run_id,
                task.batch_id,
                _candidate_key(candidate),
                json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str),
            )
            for candidate in task.candidates
        ]
        with closing(self._connect()) as con:
            with con:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO benchmark_candidates
                        (run_id, batch_id, candidate_key, candidate_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
        return PersistenceReceipt(
            run_id=task.run_id,
            batch_id=task.batch_id,
            committed_at=self._clock(),
            candidate_keys=tuple(row[2] for row in rows),
        )

    def count(self, run_id: str) -> int:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT COUNT(*) FROM benchmark_candidates WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0]) if row else 0


@dataclass
class CollectingResultWriter:
    results: list[AssessmentResult] = field(default_factory=list)

    def commit(self, result: AssessmentResult) -> None:
        self.results.append(result)


@dataclass(frozen=True)
class BenchmarkRunResult:
    run_id: str
    mode: str
    dataset_id: str
    dataset_hash: str
    task_hashes: tuple[str, ...]
    result_keys: tuple[str, ...]
    persisted_candidates: int
    metrics: dict
    events: tuple[dict, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _task_for_batch(run_id: str, batch: FrozenBatch) -> AssessmentTask:
    candidates = tuple(batch.jobs)
    task_payload = {
        "batch_id": batch.batch_id,
        "candidates": candidates,
    }
    return AssessmentTask(
        run_id=run_id,
        batch_id=batch.batch_id,
        candidates=candidates,
        task_hash=canonical_hash(task_payload),
    )


def _metrics_dict(metrics: SchedulerMetrics) -> dict:
    return {
        "total_elapsed": metrics.total_elapsed,
        "producer_elapsed": metrics.producer_elapsed,
        "consumer_elapsed": metrics.consumer_elapsed,
        "overlap_elapsed": metrics.overlap_elapsed,
        "time_to_first_result": metrics.time_to_first_result,
        "queue_peak": metrics.queue_peak,
        "queue_wait_avg": metrics.queue_wait_avg,
        "queue_wait_p50": metrics.queue_wait_p50,
        "queue_wait_p95": metrics.queue_wait_p95,
        "processed_batches": metrics.processed_batches,
        "processed_items": metrics.processed_items,
    }


def run_replay_benchmark(
    dataset: FrozenDataset,
    *,
    mode: Literal["serial", "streaming"],
    database_path: str | Path,
    assessment_engine: AssessmentEngine,
    timing_mode: TimingMode = "recorded",
    speed_factor: float = 1.0,
    run_id: str | None = None,
) -> BenchmarkRunResult:
    """Run one isolated replay with persistence before assessment."""
    effective_run_id = run_id or uuid4().hex
    repository = IsolatedCandidateRepository(database_path)
    writer = CollectingResultWriter()
    source = ReplayBatchSource(dataset, timing_mode=timing_mode, speed_factor=speed_factor)
    run_started_at = time.monotonic()
    events: list[dict] = [{"event": "run_started", "offset_seconds": 0.0}]

    def _record(event: str, batch_id: str = "", at: float | None = None) -> None:
        events.append(
            {
                "event": event,
                "batch_id": batch_id,
                "offset_seconds": (at if at is not None else time.monotonic()) - run_started_at,
            }
        )

    def _persisted_tasks() -> Iterator[ScheduledBatch[AssessmentTask]]:
        for batch in source.iter_batches():
            _record("batch_ready", batch.batch_id)
            task = _task_for_batch(effective_run_id, batch)
            receipt = repository.persist(task)
            _record("persistence_committed", batch.batch_id, receipt.committed_at)
            persisted_task = replace(task, persistence_receipt=receipt)
            yield ScheduledBatch(
                batch_id=batch.batch_id,
                value=persisted_task,
                ready_at=receipt.committed_at,
                item_count=len(task.candidates),
            )

    def _assess(batch: ScheduledBatch[AssessmentTask]) -> None:
        result = assessment_engine.assess(batch.value)
        _record("assessment_started", batch.batch_id, result.started_at)
        _record("assessment_finished", batch.batch_id, result.finished_at)
        writer.commit(result)
        _record("result_committed", batch.batch_id)

    metrics = BatchScheduler[AssessmentTask](mode).run(_persisted_tasks(), _assess)
    _record("pipeline_finished")
    return BenchmarkRunResult(
        run_id=effective_run_id,
        mode=mode,
        dataset_id=dataset.dataset_id,
        dataset_hash=dataset.dataset_hash,
        task_hashes=tuple(result.task_hash for result in writer.results),
        result_keys=tuple(key for result in writer.results for key in result.candidate_keys),
        persisted_candidates=repository.count(effective_run_id),
        metrics=_metrics_dict(metrics),
        events=tuple(sorted(events, key=lambda event: float(event["offset_seconds"]))),
    )


def _aggregate(runs: Sequence[BenchmarkRunResult]) -> dict:
    totals = [float(run.metrics["total_elapsed"]) for run in runs]
    first_results = [
        float(run.metrics["time_to_first_result"])
        for run in runs
        if run.metrics["time_to_first_result"] is not None
    ]
    overlaps = [float(run.metrics["overlap_elapsed"]) for run in runs]
    return {
        "runs": len(runs),
        "total_mean": statistics.mean(totals),
        "total_p50": statistics.median(totals),
        "total_p95": _percentile(totals, 0.95),
        "first_result_mean": statistics.mean(first_results) if first_results else None,
        "first_result_p50": statistics.median(first_results) if first_results else None,
        "first_result_p95": _percentile(first_results, 0.95) if first_results else None,
        "overlap_mean": statistics.mean(overlaps),
    }


def _improvement(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None or baseline == 0:
        return None
    return (baseline - candidate) / baseline * 100


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot calculate a percentile from no values")
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bootstrap_mean_ci(values: Sequence[float], *, samples: int = 2000) -> list[float] | None:
    if not values:
        return None
    if len(values) == 1:
        return [values[0], values[0]]
    rng = random.Random(0)
    means = [
        statistics.mean(rng.choice(values) for _ in values)
        for _sample in range(samples)
    ]
    return [_percentile(means, 0.025), _percentile(means, 0.975)]


def run_paired_benchmark(
    dataset: FrozenDataset,
    *,
    output_directory: str | Path,
    runs: int = 5,
    warmups: int = 1,
    assessment_delay_seconds: float = 0.04,
    timing_mode: TimingMode = "recorded",
    speed_factor: float = 1.0,
) -> dict:
    """Alternate serial/streaming order and write raw plus aggregate reports."""
    if runs <= 0:
        raise ValueError("runs must be greater than zero")
    if warmups < 0:
        raise ValueError("warmups must be non-negative")
    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)

    def _one(mode: Literal["serial", "streaming"], pair_index: int, warmup: bool) -> BenchmarkRunResult:
        with tempfile.TemporaryDirectory(prefix="jobradar-pipeline-benchmark-") as temp_dir:
            return run_replay_benchmark(
                dataset,
                mode=mode,
                database_path=Path(temp_dir) / "benchmark.db",
                assessment_engine=DelayAssessmentEngine(assessment_delay_seconds),
                timing_mode=timing_mode,
                speed_factor=speed_factor,
                run_id=f"{'warmup' if warmup else 'run'}-{pair_index}-{mode}-{uuid4().hex[:8]}",
            )

    for pair_index in range(warmups):
        order = ("serial", "streaming") if pair_index % 2 == 0 else ("streaming", "serial")
        for mode in order:
            _one(mode, pair_index, True)

    collected: dict[str, list[BenchmarkRunResult]] = {"serial": [], "streaming": []}
    raw_runs: list[BenchmarkRunResult] = []
    paired_total_improvements: list[float] = []
    paired_first_result_improvements: list[float] = []
    equivalent = True
    for pair_index in range(runs):
        order = ("serial", "streaming") if pair_index % 2 == 0 else ("streaming", "serial")
        pair: dict[str, BenchmarkRunResult] = {}
        for mode in order:
            result = _one(mode, pair_index, False)
            collected[mode].append(result)
            raw_runs.append(result)
            pair[mode] = result
        equivalent = equivalent and (
            pair["serial"].dataset_hash == pair["streaming"].dataset_hash
            and pair["serial"].task_hashes == pair["streaming"].task_hashes
            and pair["serial"].result_keys == pair["streaming"].result_keys
            and pair["serial"].persisted_candidates == pair["streaming"].persisted_candidates
        )
        total_improvement = _improvement(
            float(pair["serial"].metrics["total_elapsed"]),
            float(pair["streaming"].metrics["total_elapsed"]),
        )
        serial_first = pair["serial"].metrics["time_to_first_result"]
        streaming_first = pair["streaming"].metrics["time_to_first_result"]
        first_improvement = _improvement(
            float(serial_first) if serial_first is not None else None,
            float(streaming_first) if streaming_first is not None else None,
        )
        if total_improvement is not None:
            paired_total_improvements.append(total_improvement)
        if first_improvement is not None:
            paired_first_result_improvements.append(first_improvement)

    serial_summary = _aggregate(collected["serial"])
    streaming_summary = _aggregate(collected["streaming"])
    report = {
        "schema_version": 1,
        "dataset_id": dataset.dataset_id,
        "dataset_hash": dataset.dataset_hash,
        "timing_mode": timing_mode,
        "speed_factor": speed_factor,
        "assessment_delay_seconds": assessment_delay_seconds,
        "warmups": warmups,
        "result_equivalent": equivalent,
        "serial": serial_summary,
        "streaming": streaming_summary,
        "improvement_percent": {
            "total_mean": _improvement(serial_summary["total_mean"], streaming_summary["total_mean"]),
            "total_p50": _improvement(serial_summary["total_p50"], streaming_summary["total_p50"]),
            "first_result_mean": _improvement(
                serial_summary["first_result_mean"], streaming_summary["first_result_mean"]
            ),
            "first_result_p50": _improvement(
                serial_summary["first_result_p50"], streaming_summary["first_result_p50"]
            ),
        },
        "paired_improvement_percent": {
            "total_mean": statistics.mean(paired_total_improvements) if paired_total_improvements else None,
            "total_mean_ci95": _bootstrap_mean_ci(paired_total_improvements),
            "first_result_mean": (
                statistics.mean(paired_first_result_improvements)
                if paired_first_result_improvements
                else None
            ),
            "first_result_mean_ci95": _bootstrap_mean_ci(paired_first_result_improvements),
        },
    }

    raw_path = output_dir / "pipeline_benchmark_runs.jsonl"
    raw_path.write_text(
        "".join(json.dumps(result.to_dict(), ensure_ascii=False) + "\n" for result in raw_runs),
        encoding="utf-8",
    )
    report_path = output_dir / "pipeline_benchmark_summary.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare serial and streaming assessment scheduling")
    parser.add_argument("--dataset", required=True, help="Frozen post-filter batch dataset (.json or .jsonl)")
    parser.add_argument("--output-dir", default="reports/pipeline_benchmark")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--assessment-delay", type=float, default=0.04)
    parser.add_argument("--timing", choices=("instant", "recorded"), default="recorded")
    parser.add_argument("--speed-factor", type=float, default=1.0)
    args = parser.parse_args(argv)

    report = run_paired_benchmark(
        FrozenDataset.load(args.dataset),
        output_directory=args.output_dir,
        runs=args.runs,
        warmups=args.warmups,
        assessment_delay_seconds=args.assessment_delay,
        timing_mode=args.timing,
        speed_factor=args.speed_factor,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result_equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
