"""Reusable serial and streaming schedulers for persisted assessment batches.

The scheduler owns execution timing only. Producing, persisting, assessing, and
publishing results remain caller-provided operations so production and replay
benchmarks can share the same scheduling behavior without sharing side effects.
"""
from __future__ import annotations

import queue
import statistics
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

T = TypeVar("T")
ScheduleMode = Literal["serial", "streaming"]


@dataclass(frozen=True)
class ScheduledBatch(Generic[T]):
    """A persisted unit of work that is ready for assessment."""

    batch_id: str
    value: T
    ready_at: float
    item_count: int


@dataclass
class SchedulerMetrics:
    """Timing data produced by either scheduling policy."""

    mode: ScheduleMode
    total_elapsed: float = 0.0
    producer_elapsed: float = 0.0
    consumer_elapsed: float = 0.0
    overlap_elapsed: float = 0.0
    time_to_first_result: float | None = None
    queue_peak: int = 0
    processed_batches: int = 0
    processed_items: int = 0
    queue_waits: list[float] = field(default_factory=list)

    @property
    def queue_wait_avg(self) -> float:
        return statistics.mean(self.queue_waits) if self.queue_waits else 0.0

    @property
    def queue_wait_p50(self) -> float:
        return statistics.median(self.queue_waits) if self.queue_waits else 0.0

    @property
    def queue_wait_p95(self) -> float:
        if not self.queue_waits:
            return 0.0
        ordered = sorted(self.queue_waits)
        index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
        return ordered[index]


class BatchScheduler(Generic[T]):
    """Run identical persisted batches under a serial or streaming policy."""

    def __init__(
        self,
        mode: ScheduleMode,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in ("serial", "streaming"):
            raise ValueError(f"Unsupported scheduling mode: {mode}")
        self.mode = mode
        self._clock = clock

    def run(
        self,
        batches: Iterable[ScheduledBatch[T]],
        process: Callable[[ScheduledBatch[T]], None],
    ) -> SchedulerMetrics:
        if self.mode == "serial":
            return self._run_serial(batches, process)
        return self._run_streaming(batches, process)

    def _run_serial(
        self,
        batches: Iterable[ScheduledBatch[T]],
        process: Callable[[ScheduledBatch[T]], None],
    ) -> SchedulerMetrics:
        started_at = self._clock()
        buffered = list(batches)
        producer_finished_at = self._clock()
        metrics = SchedulerMetrics(
            mode="serial",
            producer_elapsed=producer_finished_at - started_at,
            queue_peak=len(buffered),
        )

        consumer_started_at: float | None = None
        first_result_at: float | None = None
        for batch in buffered:
            batch_started_at = self._clock()
            if consumer_started_at is None:
                consumer_started_at = batch_started_at
            metrics.queue_waits.append(batch_started_at - batch.ready_at)
            process(batch)
            finished_at = self._clock()
            if first_result_at is None:
                first_result_at = finished_at
            metrics.processed_batches += 1
            metrics.processed_items += batch.item_count

        finished_at = self._clock()
        metrics.total_elapsed = finished_at - started_at
        if consumer_started_at is not None:
            metrics.consumer_elapsed = finished_at - consumer_started_at
        if first_result_at is not None:
            metrics.time_to_first_result = first_result_at - started_at
        return metrics
    def _run_streaming(
        self,
        batches: Iterable[ScheduledBatch[T]],
        process: Callable[[ScheduledBatch[T]], None],
    ) -> SchedulerMetrics:
        started_at = self._clock()
        metrics = SchedulerMetrics(mode="streaming")
        work_queue: queue.Queue[ScheduledBatch[T] | object] = queue.Queue()
        stop = object()
        errors: list[BaseException] = []
        consumer_started_at: list[float] = []
        consumer_finished_at: list[float] = []
        first_result_at: list[float] = []

        def _worker() -> None:
            while True:
                item = work_queue.get()
                if item is stop:
                    return
                assert isinstance(item, ScheduledBatch)
                batch_started_at = self._clock()
                if not consumer_started_at:
                    consumer_started_at.append(batch_started_at)
                metrics.queue_waits.append(batch_started_at - item.ready_at)
                try:
                    process(item)
                except BaseException as exc:
                    errors.append(exc)
                    return
                finished_at = self._clock()
                if not first_result_at:
                    first_result_at.append(finished_at)
                metrics.processed_batches += 1
                metrics.processed_items += item.item_count
                consumer_finished_at[:] = [finished_at]

        worker = threading.Thread(target=_worker, name="jobradar-batch-worker", daemon=True)
        worker.start()
        producer_finished_at = started_at
        try:
            for batch in batches:
                if errors:
                    raise errors[0]
                work_queue.put(batch)
                metrics.queue_peak = max(metrics.queue_peak, work_queue.qsize())
            producer_finished_at = self._clock()
        finally:
            work_queue.put(stop)
            worker.join()

        if errors:
            raise errors[0]

        finished_at = self._clock()
        metrics.total_elapsed = finished_at - started_at
        metrics.producer_elapsed = producer_finished_at - started_at
        if consumer_started_at and consumer_finished_at:
            metrics.consumer_elapsed = consumer_finished_at[0] - consumer_started_at[0]
            metrics.overlap_elapsed = max(
                0.0,
                min(producer_finished_at, consumer_finished_at[0]) - consumer_started_at[0],
            )
        if first_result_at:
            metrics.time_to_first_result = first_result_at[0] - started_at
        return metrics
