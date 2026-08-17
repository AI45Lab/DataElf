from __future__ import annotations

import re
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from dataelf.config import DEFAULT_CONFIG_FILE, DataElfConfig, write_config_template
from dataelf.discovery.explorer_factory import is_pi_family_explorer, normalize_insights_explorer_name
from dataelf.discovery.workflow import run_discovery
from dataelf.stores.sqlite_store import SQLiteStore
from dataelf.tools.registry import list_tool_specs

app = typer.Typer(help="DataElf Insight Discovery CLI")
job_app = typer.Typer(help="Inspect discovery jobs")
tools_app = typer.Typer(help="Inspect DataElf tools")
app.add_typer(job_app, name="job")
app.add_typer(tools_app, name="tools")
console = Console()


def _config() -> DataElfConfig:
    return DataElfConfig.from_env()


def _store(config: DataElfConfig) -> SQLiteStore:
    if not config.enable_sqlite:
        raise RuntimeError("SQLite job registry is disabled. Set DATAELF_ENABLE_SQLITE=1 to enable job lookup commands.")
    store = SQLiteStore(config.sqlite_path)
    store.init_schema()
    return store


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", force=True)


@app.command()
def init() -> None:
    """Initialize the local DataElf workspace."""
    config = _config()
    config.ensure_dirs()
    config_file = write_config_template(DEFAULT_CONFIG_FILE, config)
    console.print(f"Initialized DataElf workspace: [bold]{config.workspace_dir.resolve()}[/bold]")
    console.print(f"Config file: {config_file.resolve()}")
    if config.enable_sqlite:
        store = _store(config)
        store.close()
        console.print(f"SQLite: {config.sqlite_path.resolve()}")
    else:
        console.print("SQLite: disabled (set DATAELF_ENABLE_SQLITE=1 to enable job registry commands)")
    console.print(f"Raw cache: {config.raw_dir.resolve()}")
    console.print(f"Discovery workspaces: {config.workspaces_dir.resolve()}")


@app.command()
def discover(
    query: str,
    insights_explorer: str | None = typer.Option(
        None,
        "--insights-explorer",
        help="Override the configured insights explorer for this run.",
    ),
    ai_index_modeling: bool | None = typer.Option(
        None,
        "--ai-index-modeling/--no-ai-index-modeling",
        help="Enable or disable the AI Index acquisition and ontology modeling stage.",
    ),
    ontology_template: str | None = typer.Option(
        None,
        "--ontology-template",
        help="Use a fixed ontology template and skip Stage 1 model generation.",
    ),
) -> None:
    """Run a user-triggered insight discovery job."""
    _setup_logging()
    config = _config()
    updates: dict[str, object] = {}
    if insights_explorer and insights_explorer.strip():
        updates["insights_explorer"] = insights_explorer.strip()
    modeling = config.ai_index_modeling
    if ai_index_modeling is not None:
        modeling = modeling.model_copy(
            update={
                "enabled": ai_index_modeling,
                **({"ontology_template": None} if not ai_index_modeling else {}),
            }
        )
    if ontology_template and ontology_template.strip():
        modeling = modeling.model_copy(update={"ontology_template": ontology_template.strip()})
    if modeling.ontology_template and not modeling.enabled:
        raise typer.BadParameter("--ontology-template requires --ai-index-modeling")
    if modeling != config.ai_index_modeling:
        updates["ai_index_modeling"] = modeling
    if updates:
        config = config.model_copy(update=updates)
    try:
        job = run_discovery(query, config)
    except Exception as exc:
        console.print(f"[red]DataElf discover failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    status_style = "green" if job.status == "completed" else "red"
    console.print(f"[{status_style}]Discovery job {job.status}:[/{status_style}] {job.job_id}")
    workspace = Path(job.workspace_path).resolve()
    explorer_name = normalize_insights_explorer_name(config.insights_explorer)
    console.print(f"Workspace: {workspace}")
    console.print(f"Explorer: {explorer_name}")
    if is_pi_family_explorer(explorer_name):
        console.print(f"Requested model: {config.pi_model or '<pi default>'}")
        console.print(f"Pi events: {workspace / 'logs' / 'pi_events.jsonl'}")
        console.print(f"pi stdout: {workspace / 'logs' / 'pi_stdout.log'}")
        console.print(f"pi stderr: {workspace / 'logs' / 'pi_stderr.log'}")
    else:
        console.print(f"Requested model: {config.model or '<dcode default>'}")
        console.print(f"Actual dcode model: {_read_dcode_model(workspace) or '<unknown>'}")
        console.print(f"dcode stdout: {workspace / 'logs' / 'dcode_stdout.log'}")
        console.print(f"dcode stderr: {workspace / 'logs' / 'dcode_stderr.log'}")
    if config.ai_index_modeling.enabled:
        console.print(f"AI Index modeling state: {workspace / 'modeling' / 'ai_index' / 'state.json'}")
    console.print(f"Insight candidates: {workspace / 'insights' / 'insight_candidates.json'}")
    console.print(f"Final brief: {workspace / 'insights' / 'final_brief.md'}")
    console.print(f"Review file: {workspace / 'reviews' / 'quality_review.json'}")
    if config.enable_sqlite:
        console.print(f"Registry review: dataelf job review {job.job_id}")
        console.print(f"Registry logs: dataelf job logs {job.job_id}")
    if job.status == "failed":
        if job.error:
            console.print(f"[red]Error:[/red] {job.error}")
        raise typer.Exit(code=1)


@job_app.command("workspace")
def job_workspace(job_id: str) -> None:
    """Show a discovery job workspace path."""
    config = _config()
    if not config.enable_sqlite:
        _print_sqlite_disabled()
        return
    store = _store(config)
    job = store.get_discovery_job(job_id)
    if not job:
        console.print(f"[yellow]No discovery job found:[/yellow] {job_id}")
    else:
        console.print(Path(job.workspace_path).resolve())
    store.close()


@job_app.command("insights")
def job_insights(job_id: str) -> None:
    """Show a discovery job's insight_candidates.json."""
    _print_job_file(job_id, "insights/insight_candidates.json")


@job_app.command("brief")
def job_brief(job_id: str) -> None:
    """Show a discovery job's final brief."""
    _print_job_file(job_id, "insights/final_brief.md")


@job_app.command("review")
def job_review(job_id: str) -> None:
    """Show a discovery job's quality review."""
    _print_job_file(job_id, "reviews/quality_review.json")


@job_app.command("logs")
def job_logs(job_id: str) -> None:
    """Show workflow logs for a discovery job."""
    config = _config()
    if not config.enable_sqlite:
        _print_sqlite_disabled()
        return
    store = _store(config)
    events = store.list_trace_events(job_id)
    table = Table(title=f"Job Logs: {job_id}")
    table.add_column("time")
    table.add_column("event")
    table.add_column("payload")
    for event in events:
        table.add_row(event["created_at"], event["event_type"], str(event["payload"]))
    console.print(table)
    store.close()


@tools_app.command("list")
def tools_list() -> None:
    """List DataElf controlled tools."""
    table = Table(title="DataElf Tools")
    table.add_column("name")
    table.add_column("permission")
    table.add_column("description")
    for spec in list_tool_specs():
        table.add_row(spec.name, spec.permission, spec.description)
    console.print(table)


def _print_job_file(job_id: str, relative_path: str) -> None:
    config = _config()
    if not config.enable_sqlite:
        _print_sqlite_disabled()
        return
    store = _store(config)
    job = store.get_discovery_job(job_id)
    if not job:
        console.print(f"[yellow]No discovery job found:[/yellow] {job_id}")
        store.close()
        return
    path = Path(job.workspace_path) / relative_path
    if not path.exists():
        console.print(f"[yellow]Missing job artifact:[/yellow] {path}")
    else:
        console.print(path.read_text(encoding="utf-8"))
    store.close()


def _print_sqlite_disabled() -> None:
    console.print(
        "[yellow]SQLite job registry is disabled by default.[/yellow]\n"
        "Use the workspace path printed by `dataelf discover`, or set DATAELF_ENABLE_SQLITE=1 before running jobs."
    )


def _read_dcode_model(workspace: Path) -> str | None:
    for relative in ["logs/dcode_stdout.log", "logs/dcode_synthesis_retry_stdout.log"]:
        path = workspace / relative
        if not path.exists():
            continue
        match = re.search(r"\bModel:\s*([^|\n]+)", path.read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1).strip()
    return None
