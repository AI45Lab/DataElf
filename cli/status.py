import json
import sys
from pathlib import Path

import click

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from runtime import JobManager, JobStatus


#Check the status of a job.
@click.command()
@click.argument("job_id")
def status(job_id: str) -> None:
    job_manager = JobManager()
    job = job_manager.get_job(job_id)

    if job is None:
        click.echo(f"Job not found: {job_id}", err=True)
        sys.exit(1)

    # Display status
    status_symbol = {
        JobStatus.PENDING: "⏳",
        JobStatus.RUNNING: "▶️",
        JobStatus.PAUSED: "⏸️",
        JobStatus.COMPLETED: "✅",
        JobStatus.FAILED: "❌",
    }

    symbol = status_symbol.get(job.status, "❓")

    click.echo(f"Job ID: {job.job_id}")
    click.echo(f"Mode: {job.mode}")
    click.echo(f"Status: {symbol} {job.status.value}")
    click.echo(f"Task: {job.task}")
    if job.parent_asset_id:
        click.echo(f"Parent Asset: {job.parent_asset_id}")
    if job.attempt_count:
        click.echo(f"Attempts: {job.attempt_count}")
    if job.final_score:
        click.echo(f"Final Score: {job.final_score}")
    if job.clarification_turns:
        click.echo(f"Clarification: {job.clarification_status} ({job.clarification_turns} turns)")
    if job.candidate_asset_ids:
        click.echo(f"Candidates: {', '.join(job.candidate_asset_ids)}")
    if job.approval_state != "not_required":
        click.echo(f"Approval: {job.approval_state}")
    if job.checkpoint_type != "none":
        click.echo(f"Checkpoint: {job.checkpoint_type} ({job.checkpoint_state})")
        if job.checkpoint_payload:
            click.echo(f"Checkpoint Payload: {json.dumps(job.checkpoint_payload, ensure_ascii=False)}")

    if job.started_at:
        click.echo(f"Started: {job.started_at}")

    if job.completed_at:
        click.echo(f"Completed: {job.completed_at}")

    if job.error:
        click.echo(f"\nError:\n{job.error}")

    if job.capability_gap:
        click.echo(f"\nCapability Gap:\n{json.dumps(job.capability_gap, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    status()
