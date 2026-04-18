"""
全局遥测模块：记录每次 LLM 调用的 token 消耗和每个步骤的耗时。
使用方式：
    from jobradar.telemetry import telemetry
    with telemetry.timer("CV 解析"):
        result = extract_cv_profile(...)
    telemetry.print_summary()
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator

from rich import box
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass
class LLMRecord:
    step: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    elapsed: float  # 秒


@dataclass
class StepRecord:
    step: str
    elapsed: float  # 秒


class Telemetry:
    """线程安全的会话级遥测收集器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.llm_records: list[LLMRecord] = []
        self.step_records: list[StepRecord] = []
        self._session_start: float = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self.llm_records.clear()
            self.step_records.clear()
            self._session_start = time.monotonic()

    # ── LLM 记录 ─────────────────────────────────────────────────────────────

    def record_llm(
        self,
        step: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        elapsed: float,
    ) -> None:
        with self._lock:
            self.llm_records.append(LLMRecord(
                step=step,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                elapsed=elapsed,
            ))

    # ── 步骤计时 ─────────────────────────────────────────────────────────────

    @contextmanager
    def timer(self, step: str) -> Generator[None, None, None]:
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            with self._lock:
                self.step_records.append(StepRecord(step=step, elapsed=elapsed))

    # ── 汇总展示 ─────────────────────────────────────────────────────────────

    def print_summary(self) -> None:
        with self._lock:
            llm = list(self.llm_records)
            steps = list(self.step_records)

        # ── 步骤耗时表 ────────────────────────────────────────────────────────
        if steps:
            processing_total = sum(s.elapsed for s in steps)
            t = Table(
                title="步骤耗时",
                box=box.SIMPLE,
                show_header=True,
                header_style="bold cyan",
            )
            t.add_column("步骤", style="white")
            t.add_column("耗时", justify="right", style="yellow")
            for s in steps:
                t.add_row(s.step, f"{s.elapsed:.1f}s")
            t.add_row("[bold]处理合计[/bold]", f"[bold]{processing_total:.1f}s[/bold]")
            console.print(t)

        # ── LLM Token 消耗表 ──────────────────────────────────────────────────
        if llm:
            t2 = Table(
                title="LLM Token 消耗",
                box=box.SIMPLE,
                show_header=True,
                header_style="bold cyan",
            )
            t2.add_column("步骤", style="white")
            t2.add_column("模型", style="dim")
            t2.add_column("输入 tokens", justify="right", style="green")
            t2.add_column("输出 tokens", justify="right", style="blue")
            t2.add_column("耗时", justify="right", style="yellow")

            total_in = total_out = 0.0
            for r in llm:
                t2.add_row(
                    r.step,
                    f"{r.provider}/{r.model}",
                    str(r.input_tokens),
                    str(r.output_tokens),
                    f"{r.elapsed:.1f}s",
                )
                total_in += r.input_tokens
                total_out += r.output_tokens

            t2.add_row(
                "[bold]合计[/bold]", "",
                f"[bold]{int(total_in)}[/bold]",
                f"[bold]{int(total_out)}[/bold]",
                "",
            )
            console.print(t2)


# 全局单例
telemetry = Telemetry()
