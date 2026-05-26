import click

from .approve import approve
from .inspect import inspect
from .pilot import pilot
from .submit import submit
from .run import run
from .skills import skills_cmd
from .status import status
from .result import result


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    """DataElf CLI."""


cli.add_command(run)
cli.add_command(pilot)
cli.add_command(submit)
cli.add_command(status)
cli.add_command(result)
cli.add_command(approve)
cli.add_command(inspect)
cli.add_command(skills_cmd)


if __name__ == "__main__":
    cli()
