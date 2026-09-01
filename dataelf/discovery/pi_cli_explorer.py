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

from dataelf.discovery.contracts import ArtifactRef, DiscoveryContext, DiscoveryJob, ExplorerRunResult


DEFAULT_PI_MODE = "json"
DEFAULT_PI_EXTRA_ARGS = ""
DEFAULT_PI_LOG_MODE = "summary"
DEFAULT_PI_STREAM_LOGS = False
PI_LOG_MODES = {"quiet", "summary", "raw"}
NOISY_PI_EVENT_TYPES = {
    "message_start",
    "message_update",
    "message_end",
    "turn_start",
    "turn_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
}
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
        cwd: str | Path | None = None,
        timeout_seconds: int | None = None,
        extra_args: str | None = None,
        approve_project: bool = True,
        stream_logs: bool | None = None,
        log_mode: str | None = None,
    ):
        self.pi_binary = pi_binary or os.getenv("DATAELF_PI_BINARY")
        self.model = model if model is not None else os.getenv("DATAELF_PI_MODEL")
        self.mode = mode or os.getenv("DATAELF_PI_MODE", DEFAULT_PI_MODE)
        self.cwd = Path(cwd) if cwd is not None else _repo_root()
        self.timeout_seconds = timeout_seconds
        self.extra_args = extra_args if extra_args is not None else os.getenv("DATAELF_PI_EXTRA_ARGS", DEFAULT_PI_EXTRA_ARGS)
        self.approve_project = approve_project
        self.log_mode = _resolve_log_mode(log_mode=log_mode, stream_logs=stream_logs)

    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ExplorerRunResult:
        workspace_path = Path(context.workspace_path)
        workspace_path.mkdir(parents=True, exist_ok=True)
        logs_dir = workspace_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / "pi_stdout.log"
        stderr_path = logs_dir / "pi_stderr.log"
        events_path = logs_dir / "pi_events.jsonl"

        logger.info("Preparing Pi workspace: %s", workspace_path)
        if not context.prompt_path:
            return ExplorerRunResult(
                status="failed", error_code="PI_PROMPT_MISSING",
                error_message="Discovery prompt was not composed before Pi execution.",
            )
        prompt_path = Path(context.prompt_path).resolve()
        if not prompt_path.is_file() or not prompt_path.is_relative_to(workspace_path.resolve()):
            return ExplorerRunResult(
                status="failed", error_code="PI_PROMPT_INVALID",
                error_message=f"Discovery prompt is missing or outside workspace: {prompt_path}",
            )
        pi_binary = self._resolve_binary()
        if pi_binary is None:
            message = "Pi CLI not found. Install @earendil-works/pi-coding-agent, run npm install, or set DATAELF_PI_BINARY."
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(message + "\n", encoding="utf-8")
            return ExplorerRunResult(status="failed", warnings=[message], error_code=PI_BINARY_NOT_FOUND, error_message=message)

        command = self._build_command(pi_binary, prompt_path)
        env = self._build_env(workspace_path, job, context)
        timeout = self.timeout_seconds or _timeout_seconds(job)
        cwd = self.cwd.resolve()
        _write_json(logs_dir / "pi_command.json", {"command": _redact_command(command), "cwd": str(cwd), "workspace_path": str(workspace_path)})
        _write_json(logs_dir / "pi_env_redacted.json", _redact_env(env))

        logger.info("Starting Pi CLI: binary=%s mode=%s model=%s cwd=%s timeout=%ss log_mode=%s", pi_binary, self.mode, self.model or "<pi default>", cwd, timeout, self.log_mode)
        if self.log_mode == "quiet":
            logger.info("Pi raw JSON events will be captured in %s and not streamed to the terminal.", events_path)
        elif self.log_mode == "summary":
            logger.info("Pi event summaries will be streamed; raw JSON events will also be captured in %s.", events_path)
        try:
            completed = _run_pi_process(command, cwd=cwd, env=env, timeout=timeout, log_mode=self.log_mode)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\nPi CLI timed out after {timeout} seconds.\n"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            _write_json_events(stdout, events_path)
            return ExplorerRunResult(
                status="failed", artifacts=_log_artifacts(workspace_path),
                warnings=[f"Pi CLI timed out after {timeout} seconds."],
                error_code=PI_PROCESS_TIMEOUT, error_message=f"Pi CLI timed out after {timeout} seconds.",
            )

        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        event_warnings = _write_json_events(completed.stdout or "", events_path)

        if completed.returncode != 0:
            message = f"Pi CLI exited with code {completed.returncode}. See logs/pi_stderr.log."
            return ExplorerRunResult(
                status="failed", artifacts=_log_artifacts(workspace_path), warnings=[*event_warnings, message],
                error_code=f"{PI_PROCESS_NONZERO_EXIT}:{completed.returncode}", error_message=message,
            )
        if event_warnings:
            return ExplorerRunResult(
                status="failed", artifacts=_log_artifacts(workspace_path), warnings=event_warnings,
                error_code=PI_EVENT_PARSE_ERROR, error_message="Pi emitted malformed JSON events.",
            )
        return ExplorerRunResult(status="completed", artifacts=_log_artifacts(workspace_path))

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
        if self.extra_args:
            command.extend(shlex.split(self.extra_args))
        command.extend([f"@{prompt_path.resolve()}", "Run this DataElf discovery task and write the required workspace artifacts."])
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
        env["DATAELF_DOMAIN"] = context.spec.domain
        if any(artifact.kind == "ontology_rdf" for artifact in context.artifacts):
            env["DATAELF_PI_ONTOLOGY"] = "1"
        else:
            env.pop("DATAELF_PI_ONTOLOGY", None)
        if self.model:
            env["DATAELF_PI_MODEL"] = self.model
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_TELEMETRY", "0")
        return env


def _log_artifacts(workspace: Path) -> list[ArtifactRef]:
    result: list[ArtifactRef] = []
    for artifact_id, relative, media_type in [
        ("pi_stdout", "logs/pi_stdout.log", "text/plain"),
        ("pi_stderr", "logs/pi_stderr.log", "text/plain"),
        ("pi_events", "logs/pi_events.jsonl", "application/x-ndjson"),
        ("pi_command", "logs/pi_command.json", "application/json"),
    ]:
        if (workspace / relative).is_file():
            result.append(ArtifactRef(
                artifact_id=artifact_id, kind="runtime_log", path=relative, role="log",
                producer_stage="explorer", media_type=media_type,
            ))
    return result


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
    log_mode: str,
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
    stdout_thread = Thread(target=_drain_stream, args=(process.stdout, stdout_chunks, logging.INFO, "[pi] ", log_mode, True), daemon=True)
    stderr_thread = Thread(target=_drain_stream, args=(process.stderr, stderr_chunks, logging.WARNING, "[pi stderr] ", log_mode, False), daemon=True)
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


def _drain_stream(pipe: object, chunks: list[str], level: int, prefix: str, log_mode: str, summarize_json: bool) -> None:
    if pipe is None:
        return
    try:
        for line in pipe:
            chunks.append(line)
            stripped = line.rstrip()
            if not stripped or log_mode == "quiet":
                continue
            if log_mode == "raw":
                logger.log(level, "%s%s", prefix, stripped)
                continue
            if summarize_json:
                summary = _summarize_pi_event(stripped)
                if summary:
                    logger.log(level, "%s%s", prefix, summary)
            else:
                logger.log(level, "%s%s", prefix, _compact_text(stripped, limit=600))
    finally:
        close = getattr(pipe, "close", None)
        if callable(close):
            close()


def _summarize_pi_event(line: str) -> str | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return _compact_text(line, limit=600)
    if not isinstance(event, dict):
        return f"event {type(event).__name__}"

    event_type = _string(event.get("type"))
    role = _string(event.get("role"))
    usage = _summarize_usage(event.get("usage"))

    if event_type in NOISY_PI_EVENT_TYPES:
        return None

    if event_type == "session":
        cwd = _string(event.get("cwd"))
        suffix = f" cwd={cwd}" if cwd else ""
        return f"session started{suffix}"
    if event_type in {"agent_start", "agentStart"}:
        return "agent started"
    if event_type in {"agent_end", "agentEnd"}:
        return "agent finished"
    if event_type in {"toolCall", "tool_call"}:
        return _summarize_tool_call(event, usage)
    if event_type in {"toolResult", "tool_result"}:
        return _summarize_tool_result(event, usage)
    if role:
        content_summary = _summarize_content(event.get("content"))
        label = role.replace("_", " ")
        if content_summary and usage:
            return f"{label}: {content_summary} | {usage}"
        if content_summary:
            return f"{label}: {content_summary}"
        if usage:
            return f"{label}: {usage}"
        return label

    content_summary = _summarize_content(event.get("content"))
    if event_type and content_summary and usage:
        return f"{event_type}: {content_summary} | {usage}"
    if event_type and content_summary:
        return f"{event_type}: {content_summary}"
    if event_type and usage:
        return f"{event_type}: {usage}"
    if event_type:
        return event_type
    return _compact_text(json.dumps(event, ensure_ascii=False, sort_keys=True), limit=600)


def _summarize_content(content: object) -> str | None:
    if isinstance(content, str):
        return _compact_text(content, limit=220)
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            text = _compact_text(item, limit=160)
            if text:
                parts.append(text)
            continue
        if not isinstance(item, dict):
            continue
        item_type = _string(item.get("type"))
        if item_type == "text":
            text = _compact_text(_string(item.get("text")), limit=220)
            if text:
                parts.append(text)
        elif item_type in {"toolCall", "tool_call"}:
            parts.append(_summarize_tool_call(item, usage=None))
        elif item_type in {"toolResult", "tool_result"}:
            parts.append(_summarize_tool_result(item, usage=None))
        elif item_type:
            parts.append(item_type)
        if len(parts) >= 3:
            break
    return "; ".join(part for part in parts if part) or None


def _summarize_tool_call(event: dict[str, object], usage: str | None) -> str:
    name = _string(event.get("name") or event.get("toolName") or event.get("tool_name") or "tool")
    arguments = event.get("arguments") or event.get("args") or event.get("input")
    command = ""
    if isinstance(arguments, dict):
        command = _string(arguments.get("command") or arguments.get("query") or arguments.get("url"))
    elif isinstance(arguments, str):
        command = arguments
    detail = _compact_text(command, limit=220)
    summary = f"tool call: {name}"
    if detail:
        summary += f" `{detail}`"
    if usage:
        summary += f" | {usage}"
    return summary


def _summarize_tool_result(event: dict[str, object], usage: str | None) -> str:
    name = _string(event.get("toolName") or event.get("tool_name") or event.get("name") or "tool")
    content = event.get("content") or event.get("result") or event.get("output")
    if isinstance(content, (dict, list)):
        detail = _compact_text(json.dumps(content, ensure_ascii=False), limit=220)
    else:
        detail = _compact_text(_string(content), limit=220)
    summary = f"tool result: {name}"
    if detail:
        summary += f" {detail}"
    if usage:
        summary += f" | {usage}"
    return summary


def _summarize_usage(usage: object) -> str | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input")
    output_tokens = usage.get("output")
    total_tokens = usage.get("totalTokens") or usage.get("total")
    parts: list[str] = []
    if input_tokens is not None:
        parts.append(f"in={input_tokens}")
    if output_tokens is not None:
        parts.append(f"out={output_tokens}")
    if total_tokens is not None:
        parts.append(f"total={total_tokens}")
    return "tokens " + " ".join(parts) if parts else None


def _resolve_log_mode(log_mode: str | None, stream_logs: bool | None) -> str:
    raw_mode = log_mode if log_mode is not None else os.getenv("DATAELF_PI_LOG_MODE")
    if raw_mode:
        normalized = raw_mode.strip().lower()
        if normalized not in PI_LOG_MODES:
            raise ValueError(f"Unsupported Pi log mode {raw_mode!r}. Use one of: {', '.join(sorted(PI_LOG_MODES))}.")
        return normalized
    if stream_logs is not None:
        return "raw" if stream_logs else "quiet"
    if os.getenv("DATAELF_PI_STREAM_LOGS") is not None:
        return "raw" if _env_bool("DATAELF_PI_STREAM_LOGS", DEFAULT_PI_STREAM_LOGS) else "quiet"
    return DEFAULT_PI_LOG_MODE


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _compact_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


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
    minutes = job.spec.constraints.get("max_runtime_minutes", 30)
    try:
        return max(60, int(float(minutes) * 60))
    except (TypeError, ValueError):
        return 1800


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
