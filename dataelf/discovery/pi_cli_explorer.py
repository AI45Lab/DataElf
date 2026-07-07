from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from dataelf.discovery.base import DiscoveryContext, DiscoveryResult
from dataelf.discovery.prompt_builder import write_discovery_prompt
from dataelf.discovery.result_parser import parse_discovery_result
from dataelf.schemas import DiscoveryJob


DEFAULT_PI_MODE = "json"
DEFAULT_PI_EXTRA_ARGS = ""
DEFAULT_PI_STREAM_LOGS = True
PI_BINARY_NOT_FOUND = "PI_BINARY_NOT_FOUND"
PI_PROCESS_TIMEOUT = "PI_PROCESS_TIMEOUT"
PI_PROCESS_NONZERO_EXIT = "PI_PROCESS_NONZERO_EXIT"
PI_EVENT_PARSE_ERROR = "PI_EVENT_PARSE_ERROR"
logger = logging.getLogger("dataelf.discovery.pi")


class PiCliInsightsExplorer:
    """Thin Python orchestrator for Pi CLI JSON event stream mode.

    DataElf owns the job lifecycle and workspace artifact contract. Pi owns its
    own runtime, tools, skills, settings, provider auth, and execution loop.
    """

    def __init__(
        self,
        pi_binary: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        timeout_seconds: int | None = None,
        extra_args: str | None = None,
        skill_paths: list[str] | None = None,
        approve_project: bool = True,
        stream_logs: bool | None = None,
    ):
        self.pi_binary = pi_binary or os.getenv("DATAELF_PI_BINARY")
        self.model = model if model is not None else os.getenv("DATAELF_PI_MODEL")
        self.mode = mode or os.getenv("DATAELF_PI_MODE", DEFAULT_PI_MODE)
        self.timeout_seconds = timeout_seconds
        self.extra_args = extra_args if extra_args is not None else os.getenv("DATAELF_PI_EXTRA_ARGS", DEFAULT_PI_EXTRA_ARGS)
        self.skill_paths = skill_paths if skill_paths is not None else _skill_paths_from_env()
        self.approve_project = approve_project
        self.stream_logs = _env_bool("DATAELF_PI_STREAM_LOGS", DEFAULT_PI_STREAM_LOGS) if stream_logs is None else stream_logs

    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> DiscoveryResult:
        workspace_path = Path(context.workspace_path)
        workspace_path.mkdir(parents=True, exist_ok=True)
        logs_dir = workspace_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / "pi_stdout.log"
        stderr_path = logs_dir / "pi_stderr.log"
        events_path = logs_dir / "pi_events.jsonl"

        logger.info("Preparing Pi workspace: %s", workspace_path)
        prompt_path = write_discovery_prompt(job, context)
        pi_binary = self._resolve_binary()
        if pi_binary is None:
            message = "Pi CLI not found. Install @earendil-works/pi-coding-agent, run npm install, or set DATAELF_PI_BINARY."
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(message + "\n", encoding="utf-8")
            return DiscoveryResult(job_id=job.job_id, status="failed", workspace_path=str(workspace_path), warnings=[message], error=PI_BINARY_NOT_FOUND)

        command = self._build_command(pi_binary, prompt_path)
        env = self._build_env(workspace_path, job, context)
        timeout = self.timeout_seconds or _timeout_seconds(job)
        _write_json(logs_dir / "pi_command.json", {"command": _redact_command(command), "cwd": str(workspace_path)})
        _write_json(logs_dir / "pi_env_redacted.json", _redact_env(env))

        logger.info("Starting Pi CLI: binary=%s mode=%s model=%s timeout=%ss", pi_binary, self.mode, self.model or "<pi default>", timeout)
        try:
            completed = _run_pi_process(command, cwd=workspace_path, env=env, timeout=timeout, stream_logs=self.stream_logs)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\nPi CLI timed out after {timeout} seconds.\n"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            _write_json_events(stdout, events_path)
            result = parse_discovery_result(workspace_path, job_id=job.job_id)
            result.status = "failed"
            result.warnings.append(f"Pi CLI timed out after {timeout} seconds.")
            result.error = PI_PROCESS_TIMEOUT
            return result

        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        event_warnings = _write_json_events(completed.stdout or "", events_path)

        result = parse_discovery_result(workspace_path, job_id=job.job_id)
        result.warnings.extend(event_warnings)
        if completed.returncode != 0:
            result.status = "failed"
            result.warnings.append(f"Pi CLI exited with code {completed.returncode}. See logs/pi_stderr.log.")
            result.error = f"{PI_PROCESS_NONZERO_EXIT}:{completed.returncode}"
        elif event_warnings and result.error is None:
            result.error = PI_EVENT_PARSE_ERROR
            result.status = "failed"
        logger.info("Pi artifacts parsed with status=%s warnings=%s", result.status, len(result.warnings))
        return result

    def _resolve_binary(self) -> str | None:
        if self.pi_binary:
            path = Path(self.pi_binary)
            if path.is_absolute() or os.sep in self.pi_binary:
                return self.pi_binary if path.exists() else None
            return shutil.which(self.pi_binary)
        repo_root = Path(__file__).resolve().parents[2]
        local_binary = repo_root / "node_modules" / ".bin" / "pi"
        if local_binary.exists():
            return str(local_binary)
        return shutil.which("pi")

    def _build_command(self, pi_binary: str, prompt_path: Path) -> list[str]:
        command = [pi_binary, "--mode", self.mode, "--no-session"]
        if self.approve_project:
            command.append("--approve")
        if self.model:
            command.extend(["--model", self.model])
        for skill_path in self.skill_paths:
            command.extend(["--skill", skill_path])
        if self.extra_args:
            command.extend(shlex.split(self.extra_args))
        command.extend([f"@{prompt_path.relative_to(prompt_path.parents[1])}", "Run this DataElf discovery task and write the required workspace artifacts."])
        return command

    def _build_env(self, workspace_path: Path, job: DiscoveryJob, context: DiscoveryContext) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env.update({key: str(value) for key, value in context.env.items()})
        repo_root = Path(__file__).resolve().parents[2]
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) if not existing_pythonpath else f"{repo_root}{os.pathsep}{existing_pythonpath}"
        env["DATAELF_WORKSPACE"] = str(workspace_path)
        env["DATAELF_JOB_WORKSPACE"] = str(workspace_path)
        env["DATAELF_JOB_ID"] = job.job_id
        env["DATAELF_DOMAIN"] = context.domain
        if self.model:
            env["DATAELF_PI_MODEL"] = self.model
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_TELEMETRY", "0")
        return env


@dataclass
class PiCompleted:
    returncode: int
    stdout: str
    stderr: str


_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "USER",
    "LOGNAME",
    "PYTHONPATH",
    "AI_INDEX_BASE_URL",
    "AI_INDEX_API_KEY",
    "DATAELF_AI_INDEX_MODE",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "PI_CODING_AGENT_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "PI_PACKAGE_DIR",
    "PI_OFFLINE",
    "PI_SKIP_VERSION_CHECK",
    "PI_TELEMETRY",
}
_SECRET_KEY_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _run_pi_process(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    stream_logs: bool,
) -> PiCompleted:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = Thread(target=_drain_stream, args=(process.stdout, stdout_chunks, logging.INFO, "[pi] ", stream_logs), daemon=True)
    stderr_thread = Thread(target=_drain_stream, args=(process.stderr, stderr_chunks, logging.WARNING, "[pi stderr] ", stream_logs), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        raise subprocess.TimeoutExpired(command, timeout, output="".join(stdout_chunks), stderr="".join(stderr_chunks))
    stdout_thread.join()
    stderr_thread.join()
    return PiCompleted(returncode=returncode, stdout="".join(stdout_chunks), stderr="".join(stderr_chunks))


def _drain_stream(pipe: object, chunks: list[str], level: int, prefix: str, stream_logs: bool) -> None:
    if pipe is None:
        return
    try:
        for line in pipe:
            chunks.append(line)
            stripped = line.rstrip()
            if stream_logs and stripped:
                logger.log(level, "%s%s", prefix, stripped)
    finally:
        close = getattr(pipe, "close", None)
        if callable(close):
            close()


def _write_json_events(stdout: str, path: Path) -> list[str]:
    warnings: list[str] = []
    objects: list[str] = []
    for line_no, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"{PI_EVENT_PARSE_ERROR}: stdout line {line_no} is not JSON: {exc.msg}.")
            continue
        objects.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    path.write_text(("\n".join(objects) + "\n") if objects else "", encoding="utf-8")
    return warnings


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _timeout_seconds(job: DiscoveryJob) -> int:
    minutes = job.constraints.get("max_runtime_minutes", 30)
    try:
        return max(60, int(float(minutes) * 60))
    except (TypeError, ValueError):
        return 1800


def _skill_paths_from_env() -> list[str]:
    paths: list[str] = []
    raw_paths = os.getenv("DATAELF_PI_SKILL_PATHS", "")
    for item in raw_paths.split(os.pathsep):
        item = item.strip()
        if item:
            paths.append(item)
    brave_path = os.getenv("DATAELF_PI_BRAVE_SEARCH_SKILL_PATH", "").strip()
    if brave_path:
        paths.append(brave_path)
    return paths


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    return {key: _redact_value(key, value) for key, value in sorted(env.items())}


def _redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for item in command:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        redacted.append(item)
        if item == "--api-key":
            redact_next = True
    return redacted


def _redact_value(key: str, value: str) -> str:
    if any(marker in key.upper() for marker in _SECRET_KEY_MARKERS):
        return "<redacted>" if value else ""
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
