"""Rich 终端展示层。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from jobfinder.schemas import CVProfile, JobResult

console = Console()


def show_profile(profile: CVProfile) -> None:
    """展示提取出的 CVProfile，供用户确认。"""
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    table.add_column("字段", style="bold cyan", width=20)
    table.add_column("值", style="white")

    table.add_row("摘要", profile.summary)
    table.add_row("经验年限", str(profile.years_of_experience) + " 年")
    table.add_row("级别", profile.seniority)
    table.add_row("目标职位", ", ".join(profile.preferred_roles) or "-")
    table.add_row("目标地点", ", ".join(profile.preferred_locations) or "-")
    table.add_row("技能", ", ".join(profile.skills) or "-")
    table.add_row("搜索语言", profile.search_language)
    table.add_row("搜索术语", ", ".join(profile.search_terms) or "-")

    console.print(Panel(table, title="[bold green]CV 解析结果[/bold green]", border_style="green"))


def show_jobs(jobs: list[JobResult]) -> None:
    """以表格形式展示职位列表。"""
    if not jobs:
        console.print("[yellow]没有找到符合条件的职位。[/yellow]")
        return

    table = Table(
        box=box.SIMPLE_HEAVY,
        show_lines=True,
        header_style="bold magenta",
        title=f"共找到 {len(jobs)} 个职位",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("职位", style="bold", max_width=28)
    table.add_column("公司", max_width=18)
    table.add_column("地点", max_width=12)
    table.add_column("分", style="bold cyan", width=4)
    table.add_column("匹配词", max_width=25)
    table.add_column("来源", style="dim", max_width=12)
    table.add_column("状态", width=6)

    for i, job in enumerate(jobs, 1):
        if job.is_possibly_closed:
            status = "[red]⛔[/red]"
        elif job.is_complete:
            status = "[green]OK[/green]"
        else:
            status = "[yellow]?[/yellow]"

        score_str = str(job.assessment.score) if job.assessment else "-"
        keywords_str = ", ".join(job.assessment.matched_keywords[:4]) if job.assessment else "-"

        sources = ", ".join(job.sources[:2])
        table.add_row(
            str(i),
            job.title,
            job.company,
            job.location or "-",
            score_str,
            keywords_str or "-",
            sources or "-",
            status,
        )

    console.print(table)


def show_job_detail(job: JobResult) -> None:
    """展示单条职位详情。"""
    lines = [
        f"[bold cyan]职位：[/bold cyan]{job.title}",
        f"[bold cyan]公司：[/bold cyan]{job.company}",
        f"[bold cyan]地点：[/bold cyan]{job.location or '-'}",
        f"[bold cyan]链接：[/bold cyan][link={job.url}]{job.url}[/link]",
        f"[bold cyan]来源：[/bold cyan]{', '.join(job.sources)}",
        f"[bold cyan]抓取时间：[/bold cyan]{job.fetched_at.strftime('%Y-%m-%d %H:%M')}",
    ]
    if job.expires_at:
        lines.append(f"[bold cyan]截止日期：[/bold cyan]{job.expires_at.strftime('%Y-%m-%d')}")
    if job.is_possibly_closed:
        lines.append("[red]⛔ 警告：该职位可能已停止招募（snippet 包含关闭信号）[/red]")
    if not job.is_complete:
        lines.append("[yellow]⚠ 该职位信息不完整[/yellow]")

    if job.assessment:
        a = job.assessment
        bar = "█" * a.score + "░" * (10 - a.score)
        lines.append(f"\n[bold cyan]匹配分：[/bold cyan]{a.score}/10  {bar}")
        if a.matched_keywords:
            lines.append(f"[bold cyan]匹配关键词：[/bold cyan]{', '.join(a.matched_keywords)}")

    lines.append("")
    lines.append(job.description_snippet or "（无摘要）")

    console.print(Panel("\n".join(lines), title="[bold]职位详情[/bold]", border_style="blue"))


def _job_to_markdown(job: JobResult) -> str:
    lines = [
        f"# {job.title}",
        f"**公司**：{job.company}",
        f"**地点**：{job.location or '-'}",
        f"**来源**：{', '.join(job.sources)}",
        f"**链接**：{job.url}",
        f"**抓取时间**：{job.fetched_at.strftime('%Y-%m-%d %H:%M')}",
    ]
    if job.expires_at:
        lines.append(f"**截止日期**：{job.expires_at.strftime('%Y-%m-%d')}")
    lines += ["", "---", "", job.description_snippet or "（无内容）"]

    # 模型评分段落
    lines += ["", "---", "", "## 模型评分"]
    a = job.assessment
    if a:
        bar = "█" * a.score + "░" * (10 - a.score)
        lines.append(f"**整体匹配分**：{a.score}/10  `{bar}`")
        if a.matched_keywords:
            lines.append(f"\n**匹配关键词**：{', '.join(a.matched_keywords)}")
        lines.append("")
        lines.append("**优势**")
        for s in a.strengths:
            lines.append(f"- {s}")
        lines.append("")
        lines.append("**劣势 / 差距**")
        for w in a.weaknesses:
            lines.append(f"- {w}")
    else:
        lines.append("_本职位未进行 CV 匹配评估（无 CV 数据或评估被跳过）。_")

    return "\n".join(lines)


def open_job_in_editor(job: JobResult, editor: str = "vscode") -> None:
    """将职位详情写入临时 Markdown 文件并用编辑器打开。"""
    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in job.title)[:50]
    tmp_dir = Path(tempfile.gettempdir()) / "jobfinder"
    tmp_dir.mkdir(exist_ok=True)
    md_path = tmp_dir / f"{safe_name}.md"
    md_path.write_text(_job_to_markdown(job), encoding="utf-8")

    _devnull = subprocess.DEVNULL
    _is_windows = sys.platform == "win32"
    _flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if _is_windows else 0

    if editor == "vscode":
        try:
            subprocess.Popen(
                ["code", str(md_path)],
                stdin=_devnull, stdout=_devnull, stderr=_devnull,
                shell=False,
                creationflags=_flags,
            )
            console.print(f"[green]已在 VSCode 中打开：{md_path}[/green]")
            return
        except FileNotFoundError:
            console.print("[yellow]未找到 VSCode（code 命令），尝试系统默认程序...[/yellow]")

    elif editor == "obsidian":
        import urllib.parse
        uri = "obsidian://open?path=" + urllib.parse.quote(str(md_path))
        webbrowser.open(uri)
        console.print(f"[green]已发送到 Obsidian：{md_path}[/green]")
        return

    # 回退：系统默认程序（完全分离）
    import os
    os.startfile(str(md_path))
    console.print(f"[green]已用系统默认程序打开：{md_path}[/green]")


def prompt_confirm(message: str) -> bool:
    """询问用户确认（y/n）。"""
    answer = console.input(f"[bold yellow]{message} (y/n): [/bold yellow]").strip().lower()
    return answer in ("y", "yes", "")


def prompt_choice(prompt: str, options: list[str]) -> int:
    """让用户从列表中选择，返回 0-based 索引。"""
    for i, opt in enumerate(options, 1):
        console.print(f"  [cyan]{i}.[/cyan] {escape(opt)}")
    while True:
        raw = console.input(f"[bold yellow]{prompt} (1-{len(options)}): [/bold yellow]").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        console.print("[red]请输入有效的数字[/red]")
