from __future__ import annotations

import os
import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from dataelf.discovery.base import DiscoveryContext, DiscoveryResult
from dataelf.discovery.prompt_builder import resolve_discovery_prompt
from dataelf.discovery.result_parser import parse_discovery_result
from dataelf.schemas import DiscoveryJob


DEFAULT_DCODE_BINARY = "dcode"
DEFAULT_SHELL_ALLOW_LIST = "all"
DEFAULT_DCODE_EXTRA_ARGS = ""
DEFAULT_SYNTHESIS_RETRY = True
DEFAULT_STREAM_LOGS = True
logger = logging.getLogger("dataelf.discovery.dcode")
SUBAGENTS = {
    "breadth-scout": (
        "Scan AI Index tables and raw files to generate many candidate signals for technology intelligence discovery.",
        "You are the Breadth Scout for DataElf. Your job is to scan broadly and generate candidate signals. Do not produce final insights.",
    ),
    "code-analyst": (
        "Write Python scripts to analyze AI Index tables, compute aggregations, detect anomalies, and generate quantitative artifacts.",
        "You are the Code Analyst for DataElf. Your job is to write and run Python scripts under scripts/ and save outputs under tables/ and deep_dives/.",
    ),
    "web-investigator": (
        "Use web_search and fetch_url to investigate external signals that explain or challenge candidate insights.",
        "You are the Web Investigator for DataElf. Your job is to use external web signals to support or challenge candidate signals.",
    ),
    "skeptic": (
        "Challenge candidate insights by finding low-base effects, weak evidence, obviousness, missing support, and alternative explanations.",
        "You are the Skeptic for DataElf. Your job is to challenge candidate insights. Do not generate new insights.",
    ),
    "insight-synthesizer": (
        "Merge candidate signals, quantitative analysis, web findings, and skeptic review into final structured insight candidates.",
        "You are the Insight Synthesizer for DataElf. Your job is to produce final insight_candidates.json and final_brief.md.",
    ),
}


class DeepAgentsCodeCliInsightsExplorer:
    """Discovery Lab runner that delegates insights_explore to DeepAgentsCode CLI.

    This is intentionally a CLI runner for M1 validation. It is not the final
    DataElf-native agent runtime integration. The stable contract is the
    workspace artifacts, especially `insights/insight_candidates.json`.
    """

    def __init__(
        self,
        dcode_binary: str | None = None,
        shell_allow_list: str | None = None,
        auto_approve: bool = True,
        extra_args: str | None = None,
        synthesis_retry: bool | None = None,
        stream_logs: bool | None = None,
    ):
        self.dcode_binary = dcode_binary or os.getenv("DATAELF_DCODE_BINARY", DEFAULT_DCODE_BINARY)
        self.shell_allow_list = shell_allow_list or os.getenv("DATAELF_DCODE_SHELL_ALLOW_LIST", DEFAULT_SHELL_ALLOW_LIST)
        self.auto_approve = auto_approve
        self.extra_args = extra_args if extra_args is not None else os.getenv("DATAELF_DCODE_EXTRA_ARGS", DEFAULT_DCODE_EXTRA_ARGS)
        self.synthesis_retry = DEFAULT_SYNTHESIS_RETRY if synthesis_retry is None else synthesis_retry
        self.stream_logs = _env_bool("DATAELF_DCODE_STREAM_LOGS", DEFAULT_STREAM_LOGS) if stream_logs is None else stream_logs

    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> DiscoveryResult:
        workspace_path = Path(context.workspace_path)
        workspace_path.mkdir(parents=True, exist_ok=True)
        logs_dir = workspace_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / "dcode_stdout.log"
        stderr_path = logs_dir / "dcode_stderr.log"

        logger.info("Preparing DeepAgentsCode workspace: %s", workspace_path)
        prompt_path = resolve_discovery_prompt(job, context)
        logger.info("Discovery prompt written: %s", prompt_path)
        self._init_project_agents(workspace_path)
        command = self._build_command(prompt_path.read_text(encoding="utf-8"), context.model)
        env = self._build_env(workspace_path, context)
        timeout = _timeout_seconds(job)

        logger.info("Starting DeepAgentsCode CLI: binary=%s model=%s timeout=%ss", self.dcode_binary, context.model or "<dcode default>", timeout)
        try:
            completed = _run_dcode_process(
                command,
                cwd=workspace_path,
                env=env,
                timeout=timeout,
                stream_logs=self.stream_logs,
            )
        except FileNotFoundError:
            message = "DeepAgentsCode CLI not found. Please install deepagents-code and ensure dcode is on PATH, or set DATAELF_DCODE_BINARY."
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(message + "\n", encoding="utf-8")
            return DiscoveryResult(job_id=job.job_id, status="failed", workspace_path=str(workspace_path), warnings=[message], error=message)
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text((exc.stderr or "") + f"\nDeepAgentsCode CLI timed out after {timeout} seconds.\n", encoding="utf-8")
            result = parse_discovery_result(workspace_path, job_id=job.job_id)
            result.status = "incomplete" if result.status == "completed" else result.status
            result.warnings.append(f"DeepAgentsCode CLI timed out after {timeout} seconds.")
            if result.error is None:
                result.error = "dcode_timeout"
            return result

        logger.info("DeepAgentsCode CLI finished with exit code %s", completed.returncode)
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        actual_model = _extract_dcode_model(completed.stdout or "")
        if actual_model:
            logger.info("DeepAgentsCode reported actual model: %s", actual_model)

        result = parse_discovery_result(workspace_path, job_id=job.job_id)
        if completed.returncode != 0:
            result.warnings.append(f"DeepAgentsCode CLI exited with code {completed.returncode}. See logs/dcode_stderr.log.")
            if self.synthesis_retry and _should_retry_synthesis(workspace_path):
                logger.info("DeepAgentsCode produced partial artifacts; starting synthesis-only retry.")
                retry_result = self._run_synthesis_retry(job, context, workspace_path, env, timeout)
                retry_result.warnings.insert(
                    0,
                    f"Initial DeepAgentsCode run exited with code {completed.returncode}; synthesis-only retry was attempted.",
                )
                return retry_result
            if result.status != "completed" and result.error is None:
                result.error = f"dcode_exit_{completed.returncode}"
        logger.info("Discovery artifacts parsed with status=%s warnings=%s", result.status, len(result.warnings))
        return result

    def _run_synthesis_retry(
        self,
        job: DiscoveryJob,
        context: DiscoveryContext,
        workspace_path: Path,
        env: dict[str, str],
        timeout: int,
    ) -> DiscoveryResult:
        retry_prompt = _build_synthesis_retry_prompt(job, workspace_path)
        command = self._build_command(retry_prompt, context.model)
        stdout_path = workspace_path / "logs" / "dcode_synthesis_retry_stdout.log"
        stderr_path = workspace_path / "logs" / "dcode_synthesis_retry_stderr.log"
        retry_timeout = min(timeout, 900)
        try:
            completed = _run_dcode_process(
                command,
                cwd=workspace_path,
                env=env,
                timeout=retry_timeout,
                stream_logs=self.stream_logs,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text((exc.stderr or "") + f"\nDeepAgentsCode synthesis retry timed out after {retry_timeout} seconds.\n", encoding="utf-8")
            result = parse_discovery_result(workspace_path, job_id=job.job_id)
            result.warnings.append(f"DeepAgentsCode synthesis retry timed out after {retry_timeout} seconds.")
            if result.status != "completed" and result.error is None:
                result.error = "dcode_synthesis_retry_timeout"
            return result

        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        logger.info("DeepAgentsCode synthesis retry finished with exit code %s", completed.returncode)
        actual_model = _extract_dcode_model(completed.stdout or "")
        if actual_model:
            logger.info("DeepAgentsCode synthesis retry reported actual model: %s", actual_model)
        result = parse_discovery_result(workspace_path, job_id=job.job_id)
        if completed.returncode != 0:
            result.warnings.append(f"DeepAgentsCode synthesis retry exited with code {completed.returncode}. See logs/dcode_synthesis_retry_stderr.log.")
            if result.status != "completed" and result.error is None:
                result.error = f"dcode_synthesis_retry_exit_{completed.returncode}"
        logger.info("Synthesis retry artifacts parsed with status=%s warnings=%s", result.status, len(result.warnings))
        return result

    def _build_command(self, prompt: str, model: str | None) -> list[str]:
        command = [self.dcode_binary]
        if self.auto_approve:
            command.append("--auto-approve")
        if self.shell_allow_list:
            command.extend(["-S", self.shell_allow_list])
        if model:
            command.extend(["-M", model])
        if _needs_chat_completions_model_params(model, self.extra_args):
            command.extend(["--model-params", json.dumps({"use_responses_api": False})])
        if self.extra_args:
            command.extend(shlex.split(self.extra_args))
        command.extend(["-n", prompt])
        return command

    def _build_env(self, workspace_path: Path, context: DiscoveryContext) -> dict[str, str]:
        env = os.environ.copy()
        env.update({key: str(value) for key, value in context.env.items()})
        repo_root = Path(__file__).resolve().parents[2]
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) if not existing_pythonpath else f"{repo_root}{os.pathsep}{existing_pythonpath}"
        env["DATAELF_WORKSPACE"] = str(workspace_path)
        env["DATAELF_JOB_WORKSPACE"] = str(workspace_path)
        env["DATAELF_DOMAIN"] = context.domain
        if context.model:
            env["DATAELF_MODEL"] = context.model
        _mirror_openai_env_for_dcode(env)
        return env

    def _init_project_agents(self, workspace_path: Path) -> None:
        agents_root = workspace_path / ".deepagents" / "agents"
        agents_root.mkdir(parents=True, exist_ok=True)
        for name, (description, body) in SUBAGENTS.items():
            agent_dir = agents_root / name
            agent_dir.mkdir(parents=True, exist_ok=True)
            path = agent_dir / "AGENTS.md"
            if path.exists():
                continue
            path.write_text(
                "\n".join(
                    [
                        "---",
                        f"name: {name}",
                        f"description: {description}",
                        "---",
                        "",
                        body,
                        "",
                    ]
                ),
                encoding="utf-8",
            )


@dataclass
class DcodeCompleted:
    returncode: int
    stdout: str
    stderr: str


def _run_dcode_process(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    stream_logs: bool,
) -> DcodeCompleted:
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

    stdout_thread = Thread(
        target=_drain_stream,
        args=(process.stdout, stdout_chunks, logging.INFO, "[dcode] ", stream_logs),
        daemon=True,
    )
    stderr_thread = Thread(
        target=_drain_stream,
        args=(process.stderr, stderr_chunks, logging.WARNING, "[dcode stderr] ", stream_logs),
        daemon=True,
    )
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
    return DcodeCompleted(returncode=returncode, stdout="".join(stdout_chunks), stderr="".join(stderr_chunks))


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


def _timeout_seconds(job: DiscoveryJob) -> int:
    minutes = job.constraints.get("max_runtime_minutes", 30)
    try:
        return max(60, int(float(minutes) * 60))
    except (TypeError, ValueError):
        return 1800


def _should_retry_synthesis(workspace_path: Path) -> bool:
    if _has_insight_candidates(workspace_path):
        return False
    candidate_path = workspace_path / "insights" / "candidate_signals.json"
    if not candidate_path.exists():
        return False
    try:
        data = json.loads(candidate_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    signals = data.get("candidate_signals") if isinstance(data, dict) else None
    return isinstance(signals, list) and bool(signals)


def _has_insight_candidates(workspace_path: Path) -> bool:
    path = workspace_path / "insights" / "insight_candidates.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    insights = data.get("insight_candidates") if isinstance(data, dict) else None
    return isinstance(insights, list) and bool(insights)


def _build_synthesis_retry_prompt(job: DiscoveryJob, workspace_path: Path) -> str:
    return f"""# DataElf Synthesis Retry

You are running inside this existing DataElf workspace:

`{workspace_path.resolve()}`

The previous DeepAgentsCode run already performed data collection and analysis, then exited before producing valid final insight candidates.

Do not restart the full breadth scan. Do not fetch large new AI Index datasets unless absolutely necessary. First read the existing files:

- `insights/candidate_signals.json`
- `notes/*.json` and `notes/*.md`
- `tables/*.csv`
- `scripts/*.py`
- `deep_dives/*.md`
- `raw/web/*`

Your task is to finish synthesis only.

Required outputs:

1. Rewrite `insights/insight_candidates.json` with 1 to 3 strong insight candidates.
2. Rewrite `insights/final_brief.md` with a concise final brief.
3. If useful and missing, add short supporting markdown under `deep_dives/`.

Each insight must include:

- `insight_id`
- `title`
- `thesis`
- `why_now`
- `supporting_signals`
- `analysis_artifacts`
- `related_entities`
- `external_support`
- `counterarguments`
- `confidence`
- `next_questions`

Prefer existing Python scripts and tables as `analysis_artifacts`. At least one insight should connect AI Index data with an external web signal if web artifacts are available. Do not fabricate external facts; if external support is weak, say so in the insight and final brief.

User task:

`{job.seed_query or ""}`

Finish by writing the files. Keep this retry short and focused.
"""


def _extract_dcode_model(stdout: str) -> str | None:
    match = re.search(r"\bModel:\s*([^|\\n]+)", stdout)
    if not match:
        return None
    return match.group(1).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _mirror_openai_env_for_dcode(env: dict[str, str]) -> None:
    """Keep OpenAI-compatible endpoints visible to DeepAgentsCode's provider resolver.

    DeepAgentsCode can read provider-specific variables such as
    DEEPAGENTS_CODE_OPENAI_API_KEY / DEEPAGENTS_CODE_OPENAI_BASE_URL. Some
    OpenAI-compatible deployments work through the plain OPENAI_* variables in
    direct curl tests, but the dcode LangGraph server can miss them depending on
    its provider auth/config path. Mirroring avoids requiring every caller to
    export both forms.
    """

    if env.get("OPENAI_API_KEY") and not env.get("DEEPAGENTS_CODE_OPENAI_API_KEY"):
        env["DEEPAGENTS_CODE_OPENAI_API_KEY"] = env["OPENAI_API_KEY"]

    base_url = env.get("OPENAI_BASE_URL") or env.get("OPENAI_API_BASE")
    if base_url:
        env.setdefault("OPENAI_BASE_URL", base_url)
        env.setdefault("OPENAI_API_BASE", base_url)
        env.setdefault("DEEPAGENTS_CODE_OPENAI_BASE_URL", base_url)


def _needs_chat_completions_model_params(model: str | None, extra_args: str) -> bool:
    if not model or not model.startswith("openai:"):
        return False
    if "--model-params" in shlex.split(extra_args):
        return False
    return True
