from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobradar.paths import DATA_DIR


app = typer.Typer(
    no_args_is_help=True,
    help="Read persisted filter_events from data/jobradar_test_cache.db and show which jobs were filtered, at which stage, and why.",
)


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _load_latest_run(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT id, created_at, run_id, experiment_name, notes, location, roles, provider, model,
               elapsed, tokens_in, tokens_out, jobs_found, new_jobs, funnel_json
        FROM search_stats
        WHERE run_id != ''
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def _load_run(con: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT id, created_at, run_id, experiment_name, notes, location, roles, provider, model,
               elapsed, tokens_in, tokens_out, jobs_found, new_jobs, funnel_json
        FROM search_stats
        WHERE run_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()


def _load_events(con: sqlite3.Connection, run_id: str, stage: str = "", limit: int = 1000) -> list[dict[str, Any]]:
    sql = """
        SELECT id, created_at, run_id, stage, title, company, location, source, url, reason, details_json
        FROM filter_events
        WHERE run_id = ?
    """
    params: list[Any] = [run_id]
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row)
        details_json = raw.pop("details_json", None)
        raw["details"] = json.loads(details_json) if details_json else {}
        items.append(raw)
    return items


def _parse_roles(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _parse_funnel(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _format_summary(row: sqlite3.Row | None, events: list[dict[str, Any]]) -> dict[str, Any]:
    if row is None:
        return {}
    funnel = _parse_funnel(row["funnel_json"] or "")
    return {
        "search_id": row["id"],
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "experiment_name": row["experiment_name"] or "",
        "notes": row["notes"] or "",
        "location": row["location"] or "",
        "roles": _parse_roles(row["roles"] or ""),
        "provider": row["provider"] or "",
        "model": row["model"] or "",
        "elapsed": row["elapsed"] or 0,
        "tokens_in": row["tokens_in"] or 0,
        "tokens_out": row["tokens_out"] or 0,
        "jobs_found": row["jobs_found"] or 0,
        "new_jobs": row["new_jobs"] or 0,
        "funnel": funnel,
        "filter_event_count": len(events),
        "filter_stage_counts": dict(Counter(item["stage"] for item in events)),
    }


def _build_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    events = report["events"]
    lines: list[str] = []
    lines.append("# Filter Event Report")
    lines.append("")
    lines.append("## Search Summary")
    lines.append("")
    lines.append(f"- DB: `{report['db_path']}`")
    lines.append(f"- Run ID: `{summary['run_id']}`")
    lines.append(f"- Search ID: `{summary['search_id']}`")
    lines.append(f"- Created: `{summary['created_at']}`")
    if summary.get("experiment_name"):
        lines.append(f"- Experiment: `{summary['experiment_name']}`")
    if summary.get("notes"):
        lines.append(f"- Notes: {summary['notes']}")
    lines.append(f"- Location: `{summary['location']}`")
    lines.append(f"- Provider / Model: `{summary['provider']}` / `{summary['model']}`")
    lines.append(f"- Roles: {', '.join(summary['roles']) if summary['roles'] else '(none)'}")
    lines.append(f"- Jobs Found: `{summary['jobs_found']}`")
    lines.append(f"- New Jobs: `{summary['new_jobs']}`")
    lines.append(f"- Tokens: `{summary['tokens_in']}` in / `{summary['tokens_out']}` out")
    lines.append(f"- Filter Events: `{summary['filter_event_count']}`")
    stage_counts = summary.get("filter_stage_counts") or {}
    if stage_counts:
        counts_text = ", ".join(f"`{k}`={v}" for k, v in stage_counts.items())
        lines.append(f"- Stage Counts: {counts_text}")
    lines.append("")

    if summary.get("funnel"):
        lines.append("## Funnel")
        lines.append("")
        for key, value in summary["funnel"].items():
            lines.append(f"- `{key}`: `{value}`")
        lines.append("")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in events:
        grouped.setdefault(item["stage"], []).append(item)

    lines.append("## Filtered Jobs")
    lines.append("")
    if not grouped:
        lines.append("_No filter events found for this run._")
        lines.append("")
        return "\n".join(lines)

    for stage_name, stage_items in grouped.items():
        lines.append(f"### {stage_name}")
        lines.append("")
        for item in stage_items:
            title = item.get("title") or "(untitled)"
            company = item.get("company") or ""
            header = f"- **{title}**"
            if company:
                header += f" @ {company}"
            lines.append(header)
            if item.get("reason"):
                lines.append(f"  Reason: {item['reason']}")
            if item.get("location"):
                lines.append(f"  Location: `{item['location']}`")
            if item.get("source"):
                lines.append(f"  Source: `{item['source']}`")
            if item.get("url"):
                lines.append(f"  URL: `{item['url']}`")
            details = item.get("details") or {}
            if details:
                lines.append(f"  Details: `{json.dumps(details, ensure_ascii=False)}`")
            lines.append("")
    return "\n".join(lines)


@app.command()
def main(
    db_path: Path = typer.Option(DATA_DIR / "jobradar_test_cache.db", exists=True, file_okay=True, dir_okay=False),
    run_id: str = typer.Option("", help="Inspect a specific search run_id. Defaults to latest run in search_stats."),
    stage: str = typer.Option("", help="Optional stage filter, e.g. title_relevance, coarse_filter, jd_assessment."),
    limit: int = typer.Option(1000, min=1, max=5000, help="Maximum number of filter events to read."),
    json_out: bool = typer.Option(False, "--json", help="Print full JSON instead of human-readable text."),
    md_out: bool = typer.Option(False, "--md", help="Print Markdown instead of human-readable text."),
    out: Path | None = typer.Option(None, help="Optional output file path."),
) -> None:
    with _connect(db_path) as con:
        row = _load_run(con, run_id) if run_id else _load_latest_run(con)
        if row is None:
            typer.echo("No search_stats record with a non-empty run_id was found in this database.")
            raise typer.Exit(code=1)

        selected_run_id = str(row["run_id"] or "")
        events = _load_events(con, selected_run_id, stage=stage, limit=limit)
        report = {
            "db_path": str(db_path),
            "summary": _format_summary(row, events),
            "events": events,
        }

    markdown_text = _build_markdown(report)
    json_text = json.dumps(report, ensure_ascii=False, indent=2)

    if json_out:
        typer.echo(json_text)
    elif md_out:
        typer.echo(markdown_text)
    else:
        summary = report["summary"]
        typer.echo(f"DB: {db_path}")
        typer.echo(f"Run ID: {summary['run_id']}")
        typer.echo(f"Search ID: {summary['search_id']} | Created: {summary['created_at']}")
        if summary["experiment_name"]:
            typer.echo(f"Experiment: {summary['experiment_name']}")
        if summary["notes"]:
            typer.echo(f"Notes: {summary['notes']}")
        typer.echo(
            f"Location: {summary['location']} | Provider: {summary['provider']} | Model: {summary['model']}"
        )
        typer.echo(
            f"Jobs Found: {summary['jobs_found']} | New Jobs: {summary['new_jobs']} | "
            f"Tokens: {summary['tokens_in']} in / {summary['tokens_out']} out"
        )
        typer.echo(f"Roles: {', '.join(summary['roles']) if summary['roles'] else '(none)'}")
        typer.echo(f"Filter Events: {summary['filter_event_count']}")
        typer.echo(f"Stage Counts: {json.dumps(summary['filter_stage_counts'], ensure_ascii=False)}")
        typer.echo("")
        for item in events:
            header = f"[{item['stage']}] {item['title']}"
            if item.get("company"):
                header += f" @ {item['company']}"
            typer.echo(header)
            if item.get("reason"):
                typer.echo(f"  Reason: {item['reason']}")
            if item.get("location"):
                typer.echo(f"  Location: {item['location']}")
            if item.get("source"):
                typer.echo(f"  Source: {item['source']}")
            if item.get("url"):
                typer.echo(f"  URL: {item['url']}")
            details = item.get("details") or {}
            if details:
                typer.echo(f"  Details: {json.dumps(details, ensure_ascii=False)}")
            typer.echo("")

    if out:
        suffix = out.suffix.lower()
        if suffix == ".md":
            out.write_text(markdown_text, encoding="utf-8")
        else:
            out.write_text(json_text, encoding="utf-8")
        typer.echo(f"Saved filter event report to {out}")


if __name__ == "__main__":
    app()
