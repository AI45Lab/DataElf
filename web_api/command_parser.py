from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedCommand:
    raw_command: str
    mode: str
    task: str


def parse_user_command(command: str) -> ParsedCommand:
    raw_command = command.strip()
    if not raw_command:
        return ParsedCommand(raw_command=raw_command, mode="run", task="")

    tokens = _split_command(raw_command)
    if not tokens:
        return ParsedCommand(raw_command=raw_command, mode="run", task=raw_command)

    first = tokens[0].lower()
    if first == "elf" and len(tokens) > 1:
        mode = tokens[1].lower()
        if mode in {"run", "pilot", "submit"}:
            return ParsedCommand(
                raw_command=raw_command,
                mode=mode,
                task=_task_after_prefix(tokens, prefix_len=2, fallback=raw_command),
            )

    if first in {"run", "pilot", "submit"}:
        return ParsedCommand(
            raw_command=raw_command,
            mode=first,
            task=_task_after_prefix(tokens, prefix_len=1, fallback=raw_command),
        )

    return ParsedCommand(raw_command=raw_command, mode="run", task=raw_command)


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _task_after_prefix(tokens: list[str], *, prefix_len: int, fallback: str) -> str:
    if len(tokens) <= prefix_len:
        return ""
    task = " ".join(tokens[prefix_len:]).strip()
    return task or fallback.strip()
