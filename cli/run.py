import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent))

from agentic import RunCoordinator
from cli.common import bootstrap_environment
from cli.pretty import action, banner, error, focus, inspect, insight, metric, section, success, trophy, warn
from exceptions import (
    DatabaseConnectionError,
    LLMConnectionError,
    LLMError,
    PilotError,
)

@click.command()
@click.argument("task")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to config file")
@click.option("--prefix", "-p", type=str, help="Config file prefix (resolves to <prefix>-config.yaml)")
@click.option("--wait", "-w", is_flag=True, help="Wait for job completion")
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show raw LLM response and clarification transcript",
)
def run(task: str, config: str | None, prefix: str | None, wait: bool, verbose: bool) -> None:
    try:
        _run_internal(task, config, prefix, wait, verbose)
    except DatabaseConnectionError as e:
        click.echo(f"\n[Database Connection Error] {e}", err=True)
        sys.exit(1)
    except LLMConnectionError as e:
        click.echo(f"\n[LLM Connection Error] {e}", err=True)
        sys.exit(1)
    except LLMError as e:
        click.echo(f"\n[LLM Error] {e}", err=True)
        sys.exit(1)
    except PilotError as e:
        click.echo(f"\n[Error] {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n[Unexpected Error] {type(e).__name__}: {e}", err=True)
        sys.exit(1)


def _run_internal(
    task: str,
    config: str | None,
    prefix: str | None,
    wait: bool,
    verbose: bool,
) -> None:
    env = bootstrap_environment(config_path=config, prefix=prefix)
    coordinator = RunCoordinator(
        config=env["config"],
        job_manager=env["job_manager"],
        executor=env["executor"],
        registry=env["registry"],
        skill_registry=env["skill_registry"],
        llm_provider=env["llm_provider"],
    )

    banner(
        "🎯",
        "DataElf Run",
        subtitle=f"Agent: {env['config'].agent.type} | Model: {env['config'].agent.model}",
        color="blue",
    )
    action("Mode", "Single-shot execution")

    def emit_event(event: dict[str, object]) -> None:
        event_type = event.get("type")
        stage = event.get("stage")

        if event_type == "stage_started":
            if stage == "clarification":
                inspect("Clarification", "Checking required slots")
                return
            if stage == "clarification_llm":
                inspect(
                    "Clarification LLM",
                    f"Turn {event.get('turn')} · model={event.get('model')}",
                )
                return
            if stage == "execution_plan_generation":
                action("Execution Plan Generation", f"Running model={event.get('model')}")
                return
            if stage == "execution":
                action("Execution", "Running execution plan")
                return
            if stage == "capability_gap_judge":
                inspect("Capability Gap Judge", f"Running model={event.get('model')}")
                return

        if event_type == "stage_completed":
            if stage == "clarification":
                success(
                    "Clarification",
                    f"status={event.get('status')} · turns={event.get('turns', 0)}",
                )
                return
            if stage == "clarification_llm":
                llm = event.get("llm", {}) or {}
                extra = ""
                if llm.get("elapsed_seconds") is not None:
                    extra = f" elapsed={llm.get('elapsed_seconds')}s"
                success(
                    "Clarification LLM",
                    f"status={llm.get('status')} · decision={event.get('status')}{extra}",
                )
                return
            if stage == "execution_plan_generation":
                llm = event.get("llm", {}) or {}
                extra = ""
                if llm.get("elapsed_seconds") is not None:
                    extra = f" elapsed={llm.get('elapsed_seconds')}s"
                success(
                    "Execution Plan Generation",
                    f"status={llm.get('status')} · model={llm.get('model')}{extra}",
                )
                return
            if stage == "execution":
                if event.get("success"):
                    success("Execution", "status=success")
                else:
                    error("Execution", "status=failed")
                return
            if stage == "capability_gap_judge":
                capability_gap = event.get("capability_gap", {}) or {}
                insight(
                    "Capability Gap Judge",
                    f"completed · type={capability_gap.get('type')}",
                )

    response = coordinator.execute(
        task=task,
        dataset_schemas=env["dataset_schemas"],
        ask_user=wait and sys.stdin.isatty(),
        verbose=verbose,
        event_handler=emit_event,
    )
    env["trace_recorder"].finalize_job(response["job_id"])

    section("🏆", "Run Summary", color="green")
    trophy("Job ID", response["job_id"])
    clarification = response["clarification"]
    if clarification["clarification_turns"]:
        focus("Clarification Status", clarification["status"])
        metric("Clarification Turns", str(clarification["clarification_turns"]))

    if response["status"] == "needs_pilot":
        warn("Status", "needs pilot")
        cg = response["capability_gap"]
        warn("Reason", cg["reason"])
        if cg.get("fix_hint"):
            insight("Fix", cg["fix_hint"])
        if cg.get("type") == "skill_not_available":
            warn("Missing Skills", ", ".join(cg.get("missing_skills", [])))
        insight("Suggestion", "Switch to `elf pilot` for a longer autonomous loop")
        env["database"].close()
        return

    llm_metadata = response["llm_metadata"]
    metric("LLM Response", f"model={llm_metadata['model']} · time={llm_metadata['elapsed_seconds']}s")

    raw = llm_metadata.get("raw_response", "")
    if verbose and raw:
        section("🔍", "Raw LLM Response", color="cyan")
        click.echo(raw)
        click.echo("")

    if verbose:
        if clarification["clarification_turns"]:
            section("❓", "Clarification Transcript", color="yellow")
            for turn in clarification["clarification_transcript"]:
                focus("Turn", str(turn["turn"]))
                click.echo(f"   Assistant: {turn['assistant_message']}")
                click.echo(f"   User: {turn['user_reply']}")
                click.echo(f"   LLM: {turn['llm']['status']} ({turn['llm'].get('elapsed_seconds')}s)")
                if turn.get("missing_items"):
                    click.echo(f"   Missing Items: {turn['missing_items']}")
                if turn.get("suggested_defaults"):
                    click.echo(f"   Suggested Defaults: {turn['suggested_defaults']}")
                if turn.get("resolved_slots_delta"):
                    click.echo(f"   Resolved Slots Delta: {turn['resolved_slots_delta']}")
                if turn.get("guard_forced_continue"):
                    click.echo(f"   Guard: continue ({turn.get('guard_reason', 'Programmatic clarification guard')})")
                click.echo("")
            insight("Resolved Slots", str(clarification["resolved_slots"]))
            action("Resolved Task", clarification["resolved_task"])

    if (wait or verbose) and response.get("pipeline"):
        section("🔧", "Generated Execution Plan", color="blue")
        click.echo(response["pipeline"])
        click.echo("")

    execution = response["execution"]
    if execution["success"]:
        success("Status", "completed")
        result = execution["result"]
        if isinstance(result, list) and len(result) > 5:
            metric("Result", f"{len(result)} records, showing first 5:")
            for item in result[:5]:
                click.echo(f"  {item}")
            click.echo("  ...")
        else:
            metric("Result", str(result))
    else:
        error("Status", "failed")
        error("Error", str(execution["error"]))
        if response["capability_gap"]:
            section("💡", "Capability Gap", color="yellow")
            click.echo(str(response["capability_gap"]))
            insight("Suggestion", "Switch to `elf pilot` for iterative repair and derivation")

    env["database"].close()


if __name__ == "__main__":
    run()
