import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.common import bootstrap_environment


@click.command()
@click.argument("asset_id")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to config file")
@click.option("--prefix", "-p", type=str, help="Config file prefix (resolves to <prefix>-config.yaml)")
@click.option("--wait", "-w", is_flag=True, help="Wait for execution")
def submit(asset_id: str, config: str | None, prefix: str | None, wait: bool) -> None:
    env = bootstrap_environment(config_path=config, prefix=prefix)
    asset = env["asset_manager"].get_stable_asset(asset_id)
    if asset is None:
        raise click.ClickException(f"Stable asset not found: {asset_id}")
    if asset.get("asset_type") != "pipeline":
        raise click.ClickException(
            "Only stable pipeline assets are directly executable with `elf submit` in this version."
        )

    job = env["job_manager"].create_job(
        task=f"submit asset {asset_id}",
        mode="submit",
        parent_asset_id=asset_id,
    )
    env["job_manager"].update_pipeline(job.job_id, asset["pipeline"])
    click.echo(f"JobID: {job.job_id}")
    click.echo(f"Submitting stable asset: {asset_id}")

    if wait:
        result = env["executor"].execute(job.job_id, asset["pipeline"])
        click.echo(f"Status: {'completed' if result['success'] else 'failed'}")
        if result["success"]:
            click.echo(f"Result: {result['result']}")
        else:
            click.echo(f"Error: {result['error']}", err=True)

    env["database"].close()


if __name__ == "__main__":
    submit()
