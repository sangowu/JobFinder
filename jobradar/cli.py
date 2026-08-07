"""CLI 入口：Typer 命令定义。"""
from __future__ import annotations

import hashlib
import json
import os
import webbrowser
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn

from jobradar import cache
from jobradar.agent import run_search
from jobradar.assessment import gate_worker_count
from jobradar.cv_extractor import extract_cv_profile
from jobradar.cv_reader import read_cv
from jobradar.display import (
    console,
    open_job_in_editor,
    prompt_choice,
    prompt_confirm,
    show_job_detail,
    show_jobs,
    show_profile,
)
from jobradar.llm_backend import (
    AVAILABLE_MODELS,
    DEFAULT_MODELS,
    LLMConfig,
    Provider,
    check_llamacpp_connection,
    get_gemini_models,
    get_llamacpp_models,
    get_openai_models,
)
from jobradar.paths import DATA_DIR
from jobradar.runtime_config import (
    PROVIDER_KEY_MAP,
    get_effective_model,
    get_saved_defaults,
    save_env_key,
)
from jobradar.search_assessment_stage import evaluate_cached_jobs
from jobradar.telemetry import telemetry
from jobradar.tools import verify_job_active

load_dotenv()

from jobradar import __version__  # noqa: E402 — 需在 load_dotenv() 之后导入


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"jobradar v{__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="jobradar",
    help="根据你的 CV 自动搜索匹配职位",
    no_args_is_help=True,
)


@app.callback()
def _main(
    version: bool = typer.Option(False, "--version", "-V", callback=_version_callback, is_eager=True, help="显示版本号"),
) -> None:
    pass
cache_app = typer.Typer(help="缓存管理命令")
app.add_typer(cache_app, name="cache")

# ─── Preflight 检查 ───────────────────────────────────────────────────────────


def _review_roles(roles: list[str]) -> list[str]:
    """
    展示模型生成的 title 列表，允许用户删除条目或追加自定义 title，
    确认后返回最终列表。
    """
    current = list(roles)

    while True:
        console.print("\n[bold cyan]── 目标职位 Title ──────────────────────────[/bold cyan]")
        for i, r in enumerate(current, 1):
            console.print(f"  [cyan]{i}.[/cyan] {escape(r)}")
        console.print()
        console.print("  [dim]a[/dim]  添加新 title")
        console.print("  [dim]d[/dim]  删除某个 title")
        console.print("  [dim]回车[/dim]  确认并继续")

        action = console.input("[bold yellow]操作 (a/d/回车): [/bold yellow]").strip().lower()

        if action == "":
            if not current:
                console.print("[red]至少需要一个 title，请先添加。[/red]")
                continue
            break

        elif action == "a":
            raw = console.input("[yellow]输入 title（多个用逗号分隔）: [/yellow]").strip()
            added = [t.strip() for t in raw.split(",") if t.strip()]
            for t in added:
                if t not in current:
                    current.append(t)
                    console.print(f"  [green]已添加：{t}[/green]")

        elif action == "d":
            if not current:
                console.print("[red]列表为空。[/red]")
                continue
            raw = console.input(
                f"[yellow]输入要删除的序号（1-{len(current)}，多个用空格分隔）: [/yellow]"
            ).strip()
            indices = set()
            for tok in raw.split():
                if tok.isdigit() and 1 <= int(tok) <= len(current):
                    indices.add(int(tok) - 1)
            removed = [current[i] for i in sorted(indices)]
            current = [r for i, r in enumerate(current) if i not in indices]
            for r in removed:
                console.print(f"  [red]已删除：{r}[/red]")

        else:
            console.print("[red]无效操作，请输入 a、d 或直接回车。[/red]")

    return current


def _condense_search_queries(roles: list[str], max_queries: int = 3) -> list[str]:
    """
    将用户确认的 title 列表压缩为 2-3 个互不重叠的搜索词。

    原则：job site 关键词搜索会自然覆盖变体——搜 "AI Engineer" 会返回
    "Generative AI Engineer"、"Applied AI Engineer"、"LLM Engineer" 等，
    无需将每个变体单独作为查询词。

    策略：
    1. 按词数升序排列（词少的更宽泛）
    2. 如果一个 title 的所有词都被已选词覆盖，跳过（它是变体）
    3. 最多保留 max_queries 个
    """
    if not roles:
        return roles

    def tokens(title: str) -> set[str]:
        return set(title.lower().split())

    selected: list[str] = []
    # 词少（更宽泛）的优先
    for role in sorted(roles, key=lambda r: len(r.split())):
        role_tokens = tokens(role)
        # 检查是否被已选中的某个查询词完全覆盖（即变体）
        is_variant = any(tokens(s).issuperset(role_tokens) or role_tokens.issuperset(tokens(s))
                         and len(role_tokens) > len(tokens(s))
                         for s in selected)
        if not is_variant:
            selected.append(role)
        if len(selected) >= max_queries:
            break

    return selected if selected else roles[:max_queries]


def _preflight(provider: Provider) -> None:
    if provider == "ollama":
        if not check_llamacpp_connection():
            console.print("[red]无法连接 llama.cpp，请确认服务已启动（默认 http://localhost:8080）。[/red]")
            raise typer.Exit(1)
    else:
        env_key = PROVIDER_KEY_MAP.get(provider, "")
        if env_key and not os.getenv(env_key):
            key = console.input(f"[yellow]请输入 {env_key}: [/yellow]").strip()
            if not key:
                console.print("[red]API Key 不能为空。[/red]")
                raise typer.Exit(1)
            save_env_key(env_key, key)
            os.environ[env_key] = key


@app.command()
def model() -> None:
    """交互式选择默认 LLM provider 和模型，保存后自动用于 find / parse。"""
    saved_provider, saved_model = get_saved_defaults()

    console.print(f"\n[dim]当前默认：{saved_provider} / {saved_model}[/dim]\n")

    # Step 1：选择 provider
    all_providers: list[Provider] = list(AVAILABLE_MODELS.keys())
    provider_labels = []
    for p in all_providers:
        mark = " [green](当前)[/green]" if p == saved_provider else ""
        provider_labels.append(f"{p}{mark}")

    console.print("[bold cyan]选择 Provider：[/bold cyan]")
    p_idx = prompt_choice("", provider_labels)
    chosen_provider: Provider = all_providers[p_idx]

    # Step 2：获取该 provider 的模型列表（动态拉取）
    _fetchers = {
        "ollama": get_llamacpp_models,
        "gemini": get_gemini_models,
        "openai": get_openai_models,
    }
    if chosen_provider in _fetchers:
        with Progress(SpinnerColumn(), TextColumn("拉取模型列表..."), transient=True) as prog:
            prog.add_task("", total=None)
            model_list = _fetchers[chosen_provider]()
        if not model_list:
            if chosen_provider == "ollama":
                console.print("[yellow]未检测到 Ollama 模型，请先运行：ollama pull <model>[/yellow]")
            else:
                console.print(f"[yellow]无法从 API 获取模型列表，请检查 {chosen_provider.upper()}_API_KEY 是否已配置。[/yellow]")
            raise typer.Exit(1)
    else:
        model_list = AVAILABLE_MODELS.get(chosen_provider) or [DEFAULT_MODELS.get(chosen_provider, "")]
        model_list = [m for m in model_list if m]
        if not model_list:
            console.print(f"[yellow]{chosen_provider} 暂无可用默认模型，请用 --model 手动指定。[/yellow]")
            raise typer.Exit(1)

    # Step 3：选择模型
    default_for_provider = DEFAULT_MODELS.get(chosen_provider, model_list[0])
    model_labels = []
    for m in model_list:
        marks = []
        if m == saved_model and chosen_provider == saved_provider:
            marks.append("[green]当前[/green]")
        if m == default_for_provider:
            marks.append("[dim]推荐[/dim]")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        model_labels.append(f"{m}{suffix}")

    console.print(f"\n[bold cyan]选择 {chosen_provider} 模型：[/bold cyan]")
    m_idx = prompt_choice("", model_labels)
    chosen_model = model_list[m_idx]

    # Step 4：保存
    save_env_key("DEFAULT_PROVIDER", chosen_provider)
    save_env_key("DEFAULT_MODEL", chosen_model)
    os.environ["DEFAULT_PROVIDER"] = chosen_provider
    os.environ["DEFAULT_MODEL"] = chosen_model

    console.print(f"\n[green]已保存默认模型：{chosen_provider} / {chosen_model}[/green]")
    console.print("[dim]下次运行 find / parse 将自动使用此模型，可通过 --provider / --model 临时覆盖。[/dim]")


# ─── find 命令 ────────────────────────────────────────────────────────────────


@app.command()
def find(
    cv_path: Annotated[Path, typer.Argument(help="CV 文件路径（.docx 或 .md）")],
    provider: Annotated[Optional[Provider], typer.Option("--provider", "-p", help="LLM Provider（覆盖默认值）")] = None,
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="模型名称（覆盖默认值）")] = None,
    location: Annotated[Optional[str], typer.Option("--location", "-l", help="覆盖 CV 中的目标地点")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略缓存，强制重新搜索")] = False,
    limit: Annotated[int, typer.Option("--limit", help="展示条数")] = 20,
    verify: Annotated[bool, typer.Option("--verify", help="展示前主动验证职位链接是否仍有效（较慢）")] = False,
) -> None:
    """根据 CV 文件搜索匹配职位。"""
    telemetry.reset()

    saved_provider, saved_model = get_saved_defaults()
    effective_provider: Provider = provider or saved_provider  # type: ignore[assignment]
    _preflight(effective_provider)

    effective_model = get_effective_model(effective_provider, model, saved_model)
    llm = LLMConfig(provider=effective_provider, model=effective_model)
    console.print(f"[dim]使用模型：{llm.provider} / {llm.model}[/dim]")

    # Step 1: 读取并解析 CV
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task("读取 CV 文件...", total=None)
        try:
            cv_text = read_cv(cv_path)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task("解析 CV 信息...", total=None)
        try:
            with telemetry.timer("CV 解析"):
                profile = extract_cv_profile(
                    cv_text,
                    llm=llm,
                    use_cache=not refresh,
                )
        except Exception as e:
            console.print(f"[red]CV 解析失败：{e}[/red]")
            raise typer.Exit(1)

    # Step 2: 审阅并确认从 CV 提取出的目标 title
    profile.preferred_roles = _review_roles(profile.preferred_roles)

    # Step 3: 展示完整 CVProfile 等待确认
    show_profile(profile)

    if not prompt_confirm("以上信息是否正确？确认后开始搜索"):
        console.print("[yellow]已取消。[/yellow]")
        raise typer.Exit(0)

    target_location = location or (
        profile.preferred_locations[0] if profile.preferred_locations else None
    )
    if not target_location:
        target_location = console.input("[yellow]请输入目标搜索地点（如 London / Remote）: [/yellow]").strip()

    search_queries = _condense_search_queries(profile.preferred_roles)
    console.print(
        f"\n[dim]搜索词：{', '.join(search_queries)}[/dim]"
    )

    # Step 3: 运行搜索 Agent
    console.print(f"\n[bold]开始搜索：[/bold]{', '.join(search_queries)} @ {target_location}\n")

    search_profile = profile.model_copy(update={"preferred_roles": search_queries})
    try:
        with telemetry.timer("职位抓取与筛选"):
            dedup_keys, pipeline_stats = run_search(
                profile=search_profile,
                location=target_location,
                llm=llm,
                on_progress=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
                force_refresh=refresh,
            )
        for line in pipeline_stats.summary_lines():
            console.print(f"  [cyan]{line}[/cyan]")
    except Exception as e:
        console.print(f"[red]搜索失败：{e}[/red]")
        raise typer.Exit(1)

    # Step 4: 展示结果
    jobs = cache.get_jobs_by_keys(dedup_keys)[:limit]

    # 主动验证：对 expires_at=None 的职位逐一 fetch 检查
    if verify and jobs:
        to_verify = [j for j in jobs if j.expires_at is None]
        console.print(f"\n[dim]验证 {len(to_verify)} 个无截止日期的职位链接...[/dim]")
        inactive_urls: set[str] = set()
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as prog:
            task = prog.add_task("验证中...", total=len(to_verify))
            for job in to_verify:
                prog.update(task, description=f"验证：{job.title[:30]}")
                result = verify_job_active(job.url)
                if not result["active"]:
                    inactive_urls.add(job.url)
                    cache.record_failed_url(job.url, result["reason"])
                prog.advance(task)
        if inactive_urls:
            before = len(jobs)
            jobs = [j for j in jobs if j.url not in inactive_urls]
            console.print(f"[yellow]已过滤 {before - len(jobs)} 个失效职位。[/yellow]")

    jobs.sort(
        key=lambda j: (j.effective_score if j.effective_score is not None else -1),
        reverse=True,
    )

    console.print()
    show_jobs(jobs)
    console.print()
    telemetry.print_summary()

    if not jobs:
        raise typer.Exit(0)

    _interactive_menu(jobs)


def _interactive_menu(jobs) -> None:
    while True:
        console.print("\n[bold]操作：[/bold]")
        options = [
            "查看职位详情",
            "在浏览器打开职位链接",
            "在 VSCode 中查看完整 JD",
            "在 Obsidian 中查看完整 JD",
            "导出结果（JSON）",
            "退出",
        ]
        choice = prompt_choice("请选择操作", options)

        if choice == 0:
            idx = prompt_choice("选择职位编号", [f"{j.title} @ {j.company}" for j in jobs])
            show_job_detail(jobs[idx])
        elif choice == 1:
            idx = prompt_choice("选择职位编号", [f"{j.title} @ {j.company}" for j in jobs])
            webbrowser.open(jobs[idx].url)
        elif choice == 2:
            idx = prompt_choice("选择职位编号", [f"{j.title} @ {j.company}" for j in jobs])
            open_job_in_editor(jobs[idx], editor="vscode")
        elif choice == 3:
            idx = prompt_choice("选择职位编号", [f"{j.title} @ {j.company}" for j in jobs])
            open_job_in_editor(jobs[idx], editor="obsidian")
        elif choice == 4:
            out_path = Path("jobradar_results.json")
            out_path.write_text(
                json.dumps([j.model_dump(mode="json") for j in jobs], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            console.print(f"[green]已导出到 {out_path}[/green]")
        else:
            break


# ─── results 命令 ────────────────────────────────────────────────────────────


@app.command()
def results(
    limit: Annotated[int, typer.Option("--limit", help="展示条数")] = 20,
) -> None:
    """直接浏览缓存中最近的搜索结果，无需重新搜索。"""
    jobs = cache.get_recent_jobs(limit)
    if not jobs:
        console.print("[yellow]缓存中暂无结果，请先运行 find 命令。[/yellow]")
        raise typer.Exit(0)

    console.print()
    show_jobs(jobs)
    console.print()
    _interactive_menu(jobs)


# ─── assess 命令 ─────────────────────────────────────────────────────────────


@app.command()
def assess(
    cv_path: Annotated[Optional[Path], typer.Argument(help="CV 文件路径（可选，默认使用最近缓存的 CVProfile）")] = None,
    provider: Annotated[Optional[Provider], typer.Option("--provider", "-p", help="LLM Provider（覆盖默认值）")] = None,
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="模型名称（覆盖默认值）")] = None,
    limit: Annotated[int, typer.Option("--limit", help="最多评估条数")] = 200,
) -> None:
    """对缓存中尚未评估的 JD 单独运行 LLM 评估（无需重新抓取）。"""
    telemetry.reset()

    saved_provider, saved_model = get_saved_defaults()
    effective_provider: Provider = provider or saved_provider  # type: ignore[assignment]
    _preflight(effective_provider)

    effective_model = get_effective_model(effective_provider, model, saved_model)
    llm = LLMConfig(provider=effective_provider, model=effective_model)
    console.print(f"[dim]使用模型：{llm.provider} / {llm.model}[/dim]")

    # Step 1：获取 CVProfile 及其 cv_hash（匹配结果按 cv_hash 归属，必须一并确定）
    profile = None
    cv_hash = ""
    if cv_path is not None:
        try:
            cv_text = read_cv(cv_path)
            cv_hash = hashlib.sha256(cv_text.encode()).hexdigest()
            with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
                p.add_task("解析 CV 信息...", total=None)
                profile = extract_cv_profile(cv_text, llm=llm)
        except Exception as e:
            console.print(f"[red]CV 解析失败：{e}[/red]")
            raise typer.Exit(1)
    else:
        profile = cache.get_latest_cv_profile()
        if profile is None:
            console.print("[red]缓存中没有 CVProfile，请提供 CV 文件路径。[/red]")
            raise typer.Exit(1)
        cv_hash = cache.get_latest_cv_hash()
        console.print(f"[dim]使用缓存的 CVProfile：{profile.summary[:40]}（{profile.seniority_display}）[/dim]")

    if not cv_hash:
        console.print("[red]无法确定 CV 版本（cv_hash），请提供 CV 文件路径。[/red]")
        raise typer.Exit(1)

    # Step 2：为当前 CV 下尚无匹配结果的 JD 补算评分
    unassessed = cache.get_unassessed_jobs(limit=limit)
    if unassessed:
        console.print(f"\n[bold]待评估 JD：{len(unassessed)} 条[/bold]（CV 版本 {cv_hash[:8]}）")

        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
            p.add_task(f"评估中（共 {len(unassessed)} 条）...", total=None)
            with telemetry.timer("JD 评估"):
                succeeded, failed = evaluate_cached_jobs(
                    unassessed,
                    profile=profile,
                    llm=llm,
                    cv_hash=cv_hash,
                    workers=gate_worker_count(llm.provider),
                )

        console.print(f"[green]已更新 {succeeded} 条评估结果。[/green]")
        if failed:
            console.print(f"[yellow]{failed} 条评估失败，详见 logs/jobradar.log。[/yellow]")
    else:
        console.print("[dim]当前 CV 下所有 JD 均已评估，跳过评估步骤。[/dim]")

    # Step 3：加载全部已评估 JD 排序展示
    all_jobs = cache.get_recent_jobs(limit)
    all_jobs = [j for j in all_jobs if j.match_score is not None]
    all_jobs.sort(key=lambda j: (j.effective_score if j.effective_score is not None else -1), reverse=True)

    if not all_jobs:
        console.print("[yellow]没有可展示的已评估职位。[/yellow]")
        raise typer.Exit(0)

    console.print()
    show_jobs(all_jobs)
    console.print()
    telemetry.print_summary()

    _interactive_menu(all_jobs)


# ─── parse 命令 ──────────────────────────────────────────────────────────────


@app.command()
def parse(
    cv_path: Annotated[Path, typer.Argument(help="CV 文件路径")],
    provider: Annotated[Optional[Provider], typer.Option("--provider", "-p", help="覆盖默认 provider")] = None,
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="覆盖默认模型")] = None,
) -> None:
    """只解析 CV，展示提取结果，不执行搜索。"""
    saved_provider, saved_model = get_saved_defaults()
    effective_provider: Provider = provider or saved_provider  # type: ignore[assignment]
    effective_model = get_effective_model(effective_provider, model, saved_model)

    _preflight(effective_provider)
    console.print(f"[dim]使用模型：{effective_provider} / {effective_model}[/dim]")
    try:
        cv_text = read_cv(cv_path)
        profile = extract_cv_profile(cv_text, provider=effective_provider, model=effective_model)
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    show_profile(profile)


# ─── cache 子命令 ─────────────────────────────────────────────────────────────


@cache_app.command("clear")
def cache_clear() -> None:
    """清空所有缓存（含失败 URL 记录）。"""
    if prompt_confirm("确认清空所有缓存？此操作不可撤销"):
        cache.clear_all()
        console.print("[green]缓存已清空。[/green]")
    else:
        console.print("[yellow]已取消。[/yellow]")


@cache_app.command("clean")
def cache_clean() -> None:
    """只清理过期的 JD 和 Session。"""
    count = cache.clean_expired()
    console.print(f"[green]已清理 {count} 条过期记录。[/green]")


@cache_app.command("prune-scores")
def cache_prune_scores(
    stale_cv: Annotated[bool, typer.Option("--stale-cv", help="同时删除非当前 CV 的匹配结果")] = False,
    orphans: Annotated[bool, typer.Option("--orphans", help="同时删除职位已不存在的匹配结果")] = False,
    language: Annotated[str, typer.Option("--language", help="匹配 prompt 使用的语言")] = "zh",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """删除过时的匹配结果。被删除的职位会在下次搜索或 assess 时按当前口径重算。"""
    from jobradar.matching import match_prompt_version

    current_version = match_prompt_version(language)
    keep_cv_hash = cache.get_latest_cv_hash() if stale_cv else ""
    if stale_cv and not keep_cv_hash:
        console.print("[red]缓存中没有 CVProfile，无法确定当前 CV，--stale-cv 不可用。[/red]")
        raise typer.Exit(1)

    preview = cache.prune_job_matches(
        prompt_version=current_version,
        keep_cv_hash=keep_cv_hash,
        drop_orphans=orphans,
        dry_run=True,
    )
    if preview["total"] == 0:
        console.print("[dim]没有需要清理的匹配结果。[/dim]")
        return

    console.print(f"\n[bold]将删除 {preview['total']} 条匹配结果：[/bold]")
    console.print(f"  prompt 版本不是 {current_version}：{preview['stale_version']} 条")
    if stale_cv:
        console.print(f"  不属于当前 CV（{keep_cv_hash[:8]}）：{preview['stale_cv']} 条")
    if orphans:
        console.print(f"  职位已不存在：{preview['orphan']} 条")
    console.print("[dim]（分类可能重叠，总数为去重后的行数）[/dim]")

    if not yes and not prompt_confirm("确认删除？被删除的职位需要重新评估"):
        console.print("[yellow]已取消。[/yellow]")
        return

    result = cache.prune_job_matches(
        prompt_version=current_version,
        keep_cv_hash=keep_cv_hash,
        drop_orphans=orphans,
        dry_run=False,
    )
    console.print(f"[green]已删除 {result['deleted']} 条匹配结果。[/green]")
    console.print("[dim]运行 `jobradar assess` 可按当前口径补算。[/dim]")


# ─── serve 命令 ──────────────────────────────────────────────────────────────


@app.command()
def serve(
    host: Annotated[str,  typer.Option("--host", help="监听地址")] = "127.0.0.1",
    port: Annotated[int,  typer.Option("--port", "-p", help="监听端口")] = 8765,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="不自动打开浏览器")] = False,
    mock: Annotated[bool, typer.Option("--mock", help="测试模式：使用独立数据库（data/jobradar_test_cache.db），不污染正式缓存")] = False,
) -> None:
    """启动 Web UI（FastAPI + uvicorn），在浏览器中使用 JobRadar。"""
    import threading
    import time

    import uvicorn

    if mock:
        os.environ["JOBFINDER_MOCK"] = "1"
        os.environ["CACHE_DB_PATH"] = str(DATA_DIR / "jobradar_test_cache.db")
        console.print("[yellow]⚠ 测试模式已启用：使用独立数据库 data/jobradar_test_cache.db，正式缓存不受影响。[/yellow]")

    url = f"http://{host}:{port}"
    console.print(f"\n[bold green]JobRadar Web UI[/bold green]  →  {url}\n")
    console.print("[dim]按 Ctrl+C 停止服务[/dim]\n")

    if not no_browser:
        def _open():
            time.sleep(1.0)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        "jobradar.server:app",
        host=host,
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    app()
