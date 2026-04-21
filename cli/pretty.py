from __future__ import annotations

import click


def _stringify_cell(value: object) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if text else "-"


def table(headers: list[str], rows: list[list[object]], color: str = "white") -> None:
    normalized_rows = [[_stringify_cell(cell) for cell in row] for row in rows]
    widths = [len(_stringify_cell(header)) for header in headers]
    for row in normalized_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render_row(values: list[str]) -> str:
        cells = [f" {value.ljust(widths[index])} " for index, value in enumerate(values)]
        return "|" + "|".join(cells) + "|"

    separator = "|" + "|".join("-" * (width + 2) for width in widths) + "|"
    click.secho(render_row([_stringify_cell(header) for header in headers]), fg=color, bold=True)
    click.secho(separator, fg=color)
    for row in normalized_rows:
        click.secho(render_row(row), fg="white")


def divider(width: int = 72, color: str = "blue") -> None:
    click.secho("─" * width, fg=color)


def banner(icon: str, title: str, subtitle: str | None = None, color: str = "blue") -> None:
    divider(color=color)
    click.secho(f"{icon} {title}", fg=color, bold=True)
    if subtitle:
        click.secho(subtitle, fg="white")
    divider(color=color)


def section(icon: str, title: str, detail: str | None = None, color: str = "cyan") -> None:
    click.secho(f"{icon} {title}", fg=color, bold=True)
    if detail:
        click.secho(f"   {detail}", fg="white")


def line(icon: str, label: str, value: str | None = None, color: str = "white") -> None:
    prefix = f"{icon} " if icon else ""
    if value is None or value == "":
        click.secho(f"{prefix}{label}", fg=color)
        return
    click.secho(f"{prefix}{label}: ", fg=color, bold=True, nl=False)
    click.echo(value)


def success(label: str, value: str | None = None) -> None:
    line("✅", label, value=value, color="green")


def warn(label: str, value: str | None = None) -> None:
    line("❓", label, value=value, color="yellow")


def error(label: str, value: str | None = None) -> None:
    line("🔧", label, value=value, color="red")


def metric(label: str, value: str | None = None) -> None:
    line("", label, value=value, color="magenta")


def action(label: str, value: str | None = None) -> None:
    line("", label, value=value, color="cyan")


def focus(label: str, value: str | None = None) -> None:
    line("", label, value=value, color="blue")


def insight(label: str, value: str | None = None) -> None:
    line("💡", label, value=value, color="yellow")


def inspect(label: str, value: str | None = None) -> None:
    line("", label, value=value, color="cyan")


def trophy(label: str, value: str | None = None) -> None:
    line("🏆", label, value=value, color="green")
