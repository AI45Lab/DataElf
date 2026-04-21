import json
import sys
from pathlib import Path

import click

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from runtime import JobManager, JobStatus


@click.command()
@click.argument("job_id")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output result as JSON",
)
@click.option(
    "--artifacts",
    is_flag=True,
    help="Include artifacts in output",
)

#Get the result of a job.
def result(job_id: str, output_json: bool, artifacts: bool) -> None:
    job_manager = JobManager()
    job = job_manager.get_job(job_id)

    if job is None:
        click.echo(f"Job not found: {job_id}", err=True)
        sys.exit(1)

    if job.status != JobStatus.COMPLETED:
        click.echo(f"Job {job_id} is not completed (status: {job.status.value})", err=True)
        sys.exit(1)

    if output_json:
        # Output full result as JSON
        result_data = {
            "job_id": job.job_id,
            "task": job.task,
            "mode": job.mode,
            "status": job.status.value,
            "result": job.result,
            "attempt_count": job.attempt_count,
            "candidate_asset_ids": job.candidate_asset_ids,
            "clarification_status": job.clarification_status,
            "clarification_turns": job.clarification_turns,
            "clarification_transcript": job.clarification_transcript,
            "resolved_task": job.resolved_task,
            "resolved_slots": job.resolved_slots,
            "checkpoint_type": job.checkpoint_type,
            "checkpoint_state": job.checkpoint_state,
            "checkpoint_payload": job.checkpoint_payload,
        }
        if artifacts:
            result_data["artifacts"] = job.result.get("artifacts", {})
        click.echo(json.dumps(result_data, indent=2))
    else:
        # Human-readable output
        click.echo(f"Job ID: {job.job_id}")
        click.echo(f"Mode: {job.mode}")
        click.echo(f"Task: {job.task}")
        if job.attempt_count:
            click.echo(f"Attempts: {job.attempt_count}")
        if job.clarification_turns:
            click.echo(f"Clarification: {job.clarification_status} ({job.clarification_turns} turns)")
        if job.checkpoint_type != "none":
            click.echo(f"Checkpoint: {job.checkpoint_type} ({job.checkpoint_state})")
        click.echo(f"\nResult:")

        if isinstance(job.result, dict):
            if "result" in job.result:
                _print_result_value(job.result["result"])
            else:
                for key, value in job.result.items():
                    click.echo(f"  {key}: {value}")
        else:
            click.echo(f"  {job.result}")

        if artifacts and isinstance(job.result, dict) and "artifacts" in job.result:
            click.echo("\nArtifacts:")
            for key, value in job.result["artifacts"].items():
                click.echo(f"  {key}: {value}")
        metadata = job.result.get("metadata", {}) if isinstance(job.result, dict) else {}
        if metadata.get("pilot_summary"):
            summary = metadata["pilot_summary"]
            click.echo("\nPilot Summary:")
            click.echo(f"  best_attempt: {summary.get('best_attempt_id')}")
            click.echo(f"  final_attempt: {summary.get('final_attempt_id')}")
            click.echo(f"  candidate_count: {len(summary.get('candidate_ids', []))}")

#Print a result value with proper formatting
def _print_result_value(value: any, indent: int = 2) -> None:
    prefix = " " * indent

    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                click.echo(f"{prefix}{key}:")
                _print_result_value(val, indent + 2)
            else:
                click.echo(f"{prefix}{key}: {val}")
    elif isinstance(value, list):
        for item in value:
            _print_result_value(item, indent)
    else:
        click.echo(f"{prefix}{value}")


if __name__ == "__main__":
    result()
