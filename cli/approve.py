import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent))

from agentic import AssetManager


@click.command()
@click.argument("candidate_id")
def approve(candidate_id: str) -> None:
    asset_manager = AssetManager()
    candidate = asset_manager.approve_candidate(candidate_id)
    click.echo(f"Approved candidate: {candidate_id}")
    click.echo(f"Name: {candidate.get('name', candidate_id)}")
    click.echo(f"Asset Type: {candidate.get('asset_type', 'unknown')}")
    click.echo(f"Asset ID: {candidate.get('asset_id', candidate_id)}")
    click.echo("Candidate promoted to stable asset storage.")


if __name__ == "__main__":
    approve()
