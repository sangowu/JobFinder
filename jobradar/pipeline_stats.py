"""搜索管道各阶段统计（Pipeline Statistics）

用法：
    stats = PipelineStats()
    # 各阶段填入数字后
    for line in stats.summary_lines():
        print(line)
    stats.write_report()          # 写 reports/ 目录
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class PipelineStats:
    """记录一次完整搜索各阶段的漏斗统计数据。"""

    # ── 阶段一：抓取 ─────────────────────────────────────────────────────────
    scraped_indeed: int = 0        # Indeed 原始结果数
    scraped_linkedin: int = 0      # LinkedIn 原始结果数
    scraped_total: int = 0         # URL 去重合并后（LLM 标题过滤前）

    # ── 阶段二：LLM 标题过滤（scraper 层）────────────────────────────────────
    title_filter_in: int = 0       # 进入 LLM 标题过滤的数量
    title_filter_passed: int = 0   # 通过 LLM 标题过滤
    title_filter_out: int = 0      # 被 LLM 标题过滤淘汰

    # ── 阶段三：预筛漏斗（agent._write_scraped）──────────────────────────────
    prefilter_in: int = 0          # 进入 _write_scraped 的数量
    skip_dup: int = 0              # URL / 跨来源重复
    skip_seniority: int = 0        # 年资不符
    skip_irrelevant: int = 0       # 标题不相关（关键词）
    cache_hit: int = 0             # URL 缓存命中（已有 assessment，直接复用）
    cache_patch: int = 0           # URL 缓存命中但需补 LLM 评估
    skip_no_desc: int = 0          # 无职位描述
    skip_closed: int = 0           # 职位已关闭
    skip_exp: int = 0              # 经验年限要求超限
    skip_skill: int = 0            # 技能关键词完全不匹配

    # ── 阶段四：LLM 批量评估 ─────────────────────────────────────────────────
    llm_assessed: int = 0          # 送入 LLM 评估的数量
    llm_rejected: int = 0          # LLM 评估后拒绝

    # ── 最终结果 ─────────────────────────────────────────────────────────────
    saved: int = 0
    by_source: dict = field(default_factory=dict)  # {source: {step: count}}

    # ── 元数据 ────────────────────────────────────────────────────────────────
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # ── 衍生指标（只读属性）───────────────────────────────────────────────────

    @property
    def filter_rate(self) -> float:
        """过滤掉的无效 JD 占抓取总数的百分比：(total - saved) / total"""
        if self.scraped_total == 0:
            return 0.0
        return round((self.scraped_total - self.saved) / self.scraped_total * 100, 1)

    @property
    def llm_pass_rate(self) -> float:
        """LLM 评估通过率（百分比），仅计新评估批次"""
        if self.llm_assessed == 0:
            return 0.0
        passed = self.llm_assessed - self.llm_rejected
        return round(passed / self.llm_assessed * 100, 1)

    @property
    def llm_title_pass_rate(self) -> float:
        """LLM 标题过滤通过率（百分比）"""
        if self.title_filter_in == 0:
            return 0.0
        return round(self.title_filter_passed / self.title_filter_in * 100, 1)

    # ── 序列化 ────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """返回包含衍生指标的完整字典，供 JSON 序列化使用。"""
        d = dataclasses.asdict(self)
        d["filter_rate"] = self.filter_rate
        d["llm_pass_rate"] = self.llm_pass_rate
        d["llm_title_pass_rate"] = self.llm_title_pass_rate
        return d

    # ── 可读摘要 ──────────────────────────────────────────────────────────────

    def summary_lines(self) -> list[str]:
        """返回适合终端输出的人类可读摘要行列表。"""
        lines = ["━━━ 搜索管道统计 ━━━"]
        lines.append(
            f"  阶段一  抓取:  Indeed {self.scraped_indeed} | "
            f"LinkedIn {self.scraped_linkedin} | 合并去重 {self.scraped_total}"
        )
        if self.title_filter_in:
            lines.append(
                f"  阶段二  LLM标题过滤:  {self.title_filter_in} → "
                f"通过 {self.title_filter_passed}  淘汰 {self.title_filter_out}"
                f"  ({self.llm_title_pass_rate}%)"
            )
        skip_parts = []
        for attr, label in [
            ("skip_dup",        "去重"),
            ("skip_seniority",  "年资"),
            ("skip_irrelevant", "不相关"),
            ("cache_hit",       "缓存命中"),
            ("skip_no_desc",    "无描述"),
            ("skip_closed",     "已关闭"),
            ("skip_exp",        "经验超限"),
            ("skip_skill",      "技能不符"),
        ]:
            v = getattr(self, attr)
            if v:
                skip_parts.append(f"{label}:{v}")
        detail = f"  ({' | '.join(skip_parts)})" if skip_parts else ""
        lines.append(f"  阶段三  预筛漏斗:  {self.prefilter_in} 进入{detail}")
        lines.append(
            f"  阶段四  LLM评估:  送入 {self.llm_assessed}  拒绝 {self.llm_rejected}"
            + (f"  通过率 {self.llm_pass_rate}%" if self.llm_assessed else "")
        )
        lines.append(
            f"  最终    保存 {self.saved} / 抓取 {self.scraped_total}  "
            f"过滤无效 JD {self.filter_rate}%"
        )
        return lines

    # ── 文件写入 ──────────────────────────────────────────────────────────────

    def write_report(self, directory: str = "reports") -> str:
        """
        将统计数据写入两个文件并返回 latest JSON 路径：
          - pipeline_stats.jsonl      逐行追加，保留全量历史
          - pipeline_stats_latest.json 覆盖写入，始终为最新一次
        """
        out_dir = Path(directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()

        jsonl_path = out_dir / "pipeline_stats.jsonl"
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

        latest_path = out_dir / "pipeline_stats_latest.json"
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return str(latest_path)
