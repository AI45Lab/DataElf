from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from dataelf.config import DEFAULT_CONFIG_FILE, DataElfConfig, write_config_template
from dataelf.discovery.contracts import JobSpec
from dataelf.discovery.workflow import run_job
from dataelf.stores.sqlite_store import SQLiteStore

app = typer.Typer(help="DataElf runtime CLI")
job_app = typer.Typer(help="Inspect DataElf jobs")
app.add_typer(job_app, name="job")
console = Console()


def _config() -> DataElfConfig:
    return DataElfConfig.from_env()


def _store(config: DataElfConfig) -> SQLiteStore:
    if not config.runtime.enable_sqlite:
        raise RuntimeError("SQLite job registry is disabled. Set DATAELF_ENABLE_SQLITE=1 to enable job lookup commands.")
    store = SQLiteStore(config.runtime.sqlite_path)
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
    console.print(f"Initialized DataElf workspace: [bold]{config.runtime.workspace_dir.resolve()}[/bold]")
    console.print(f"Config file: {config_file.resolve()}")
    if config.runtime.enable_sqlite:
        store = _store(config)
        store.close()
        console.print(f"SQLite: {config.runtime.sqlite_path.resolve()}")
    else:
        console.print("SQLite: disabled (set DATAELF_ENABLE_SQLITE=1 to enable job registry commands)")
    console.print(f"Job workspaces: {config.runtime.workspaces_dir.resolve()}")


@app.command("run")
def run(
    query: str = typer.Argument(..., help="User task or research objective."),
    domain: str = typer.Option(..., "--domain", help="Domain package to execute."),
    modeling_enabled: bool | None = typer.Option(
        None,
        "--modeling/--no-modeling",
        help="Enable or disable the selected domain's modeling stage.",
    ),
    modeling_strategy: str | None = typer.Option(
        None,
        "--modeling-strategy",
        help="Domain-owned modeling strategy.",
    ),
    ontology_template: str | None = typer.Option(
        None,
        "--ontology-template",
        help="AI Index domain option: use a fixed ontology template.",
    ),
    parameter: list[str] = typer.Option(
        [],
        "--param",
        help="Domain parameter in key=value form; may be supplied more than once.",
    ),
) -> None:
    """Run one DataElf job for the selected domain."""
    _setup_logging()
    try:
        config = _config()
        if modeling_enabled is not None:
            config = _override_domain_modeling(config, domain, modeling_enabled)

        requested_template = _optional_text(ontology_template)
        if requested_template:
            if domain != "ai_index":
                raise typer.BadParameter("--ontology-template is only supported by the ai_index domain")
            if modeling_enabled is False:
                raise typer.BadParameter("--ontology-template requires --modeling")
            if modeling_enabled is None and not _domain_modeling_enabled(config, domain):
                raise typer.BadParameter("--ontology-template requires --modeling or enabled domain modeling config")
            config = _set_domain_modeling_field(config, domain, "ontology_template", requested_template)

        spec = JobSpec(
            domain=domain,
            objective=query,
            parameters=_parse_parameters(parameter),
            modeling_strategy=_optional_text(modeling_strategy),
        )
        job = run_job(spec, config)
    except typer.BadParameter:
        raise
    except Exception as exc:
        console.print(f"[red]DataElf run failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    status_style = "green" if job.status == "completed" else "red"
    console.print(f"[{status_style}]DataElf job {job.status}:[/{status_style}] {job.job_id}")
    workspace = Path(job.workspace_path).resolve()
    console.print(f"Workspace: {workspace}")
    console.print("Explorer: pi")
    console.print(f"Requested model: {config.explorer.pi.model or '<pi default>'}")
    console.print(f"Pi events: {workspace / 'logs' / 'pi_events.jsonl'}")
    console.print(f"pi stdout: {workspace / 'logs' / 'pi_stdout.log'}")
    console.print(f"pi stderr: {workspace / 'logs' / 'pi_stderr.log'}")
    output_artifacts = [artifact for artifact in job.artifacts if artifact.role == "output"]
    if output_artifacts:
        for artifact in output_artifacts:
            console.print(f"Output {artifact.artifact_id}: {workspace / artifact.path}")
    else:
        console.print(f"Partial outputs (if any): {workspace}")
    console.print(f"Review file: {workspace / 'reviews' / 'quality_review.json'}")
    if config.runtime.enable_sqlite:
        console.print(f"Registry review: dataelf job review {job.job_id}")
        console.print(f"Registry logs: dataelf job logs {job.job_id}")
    if job.status == "failed":
        if job.error_code:
            console.print(f"[red]Error:[/red] {job.error_code}: {job.error_message or ''}")
        raise typer.Exit(code=1)


@job_app.command("workspace")
def job_workspace(job_id: str) -> None:
    """Show a job workspace path."""
    config = _config()
    if not config.runtime.enable_sqlite:
        _print_sqlite_disabled()
        return
    store = _store(config)
    job = store.get_discovery_job(job_id)
    if not job:
        console.print(f"[yellow]No job found:[/yellow] {job_id}")
    else:
        console.print(Path(job.workspace_path).resolve())
    store.close()


@job_app.command("artifacts")
def job_artifacts(job_id: str) -> None:
    """Show a job's artifact manifest."""
    _print_job_file(job_id, "artifact_manifest.json")


@job_app.command("file")
def job_file(job_id: str, relative_path: str) -> None:
    """Show a file under a job workspace."""
    _print_job_file(job_id, relative_path)


@job_app.command("review")
def job_review(job_id: str) -> None:
    """Show a job's quality review."""
    _print_job_file(job_id, "reviews/quality_review.json")


@job_app.command("logs")
def job_logs(job_id: str) -> None:
    """Show workflow logs for a job."""
    config = _config()
    if not config.runtime.enable_sqlite:
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


def _print_job_file(job_id: str, relative_path: str) -> None:
    config = _config()
    if not config.runtime.enable_sqlite:
        _print_sqlite_disabled()
        return
    store = _store(config)
    job = store.get_discovery_job(job_id)
    if not job:
        console.print(f"[yellow]No job found:[/yellow] {job_id}")
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
        "Use the workspace path printed by `dataelf run`, or set DATAELF_ENABLE_SQLITE=1 before running jobs."
    )


def _override_domain_modeling(config: DataElfConfig, domain: str, enabled: bool) -> DataElfConfig:
    return _set_domain_modeling_field(config, domain, "enabled", enabled)


def _set_domain_modeling_field(config: DataElfConfig, domain: str, field: str, value: Any) -> DataElfConfig:
    domains = dict(config.domains)
    domain_values = dict(domains.get(domain, {}))
    raw_modeling = domain_values.get("modeling", {})
    if raw_modeling is None:
        raw_modeling = {}
    if not isinstance(raw_modeling, dict):
        raise typer.BadParameter(f"domains.{domain}.modeling must be a mapping/object")
    modeling = dict(raw_modeling)
    modeling[field] = value
    domain_values["modeling"] = modeling
    domains[domain] = domain_values
    return config.model_copy(update={"domains": domains})


def _parse_parameters(values: list[str]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for raw in values:
        key, separator, value = raw.partition("=")
        key = key.strip()
        if not separator or not key:
            raise typer.BadParameter(f"--param must use key=value form: {raw!r}")
        try:
            parameters[key] = json.loads(value)
        except json.JSONDecodeError:
            parameters[key] = value
    return parameters


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _domain_modeling_enabled(config: DataElfConfig, domain: str) -> bool:
    raw_domain = config.domain_config(domain)
    raw_modeling = raw_domain.get("modeling", {})
    configured = raw_modeling.get("enabled", False) if isinstance(raw_modeling, dict) else False
    if domain == "ai_index" and os.getenv("DATAELF_AI_INDEX_MODELING_ENABLED") is not None:
        configured = os.environ["DATAELF_AI_INDEX_MODELING_ENABLED"]
    if isinstance(configured, bool):
        return configured
    return str(configured).strip().lower() in {"1", "true", "yes", "on"}
