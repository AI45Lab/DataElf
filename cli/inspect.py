import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent))

from agentic import AssetManager
from runtime import JobManager


@click.command()
@click.argument("target_id")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def inspect(target_id: str, as_json: bool) -> None:
    job_manager = JobManager()
    asset_manager = AssetManager()

    payload = None
    job = job_manager.get_job(target_id)
    if job is not None:
        payload = job.to_dict() | {"attempts": asset_manager.list_attempts(target_id)}
    else:
        candidate = asset_manager.get_candidate(target_id)
        if candidate is not None:
            payload = candidate
        else:
            asset = asset_manager.get_stable_asset(target_id)
            if asset is not None:
                payload = asset

    if payload is None:
        raise click.ClickException(f"No job, candidate, or asset found for: {target_id}")

    if as_json:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    inspect()
