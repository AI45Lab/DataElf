"""Server completion policy around the shared Pi process executor."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from dataelf.discovery.artifacts import validate_outputs, ArtifactContractError
from dataelf.discovery.contracts import ArtifactRef, ExplorerRunResult
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer
from dataelf.discovery.run_control import check_cancelled
from dataelf_server.analysis.rdf_analysis import validate_analysis_manifest
from dataelf_server.analysis.validation import PipelineError, workspace_insights
from dataelf_server.prompts.synthesis import _build_synthesis_retry_prompt
from dataelf_server.workflows.profile import ServerProfile
from dataelf_server.runtime.agent import isolated_agent


class ServerExplorer:
    def __init__(self, settings, *, runner_factory=PiCliInsightsExplorer):
        self.settings = settings
        self.runner_factory = runner_factory

    def _runner(self, scope, retry=False):
        pi = self.settings.core.explorer.pi
        extension = Path(__file__).resolve().parents[1] / "runtime/server.ts"
        # Explicit resources isolate server behavior from project research/Fusion extensions.
        args = shlex.split(pi.extra_args or "")
        args.extend(["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "-e", str(extension)])
        return self.runner_factory(pi_binary=pi.binary, model=pi.model, mode=pi.mode,
            cwd=self.settings.project_root,
            timeout_seconds=self.settings.server.pi.synthesis_retry_timeout_seconds if retry else pi.timeout_seconds,
            extra_args=shlex.join(args), log_mode=pi.log_mode,
            log_prefix="pi_synthesis_retry" if retry else "pi", compact_stream_events=True, detect_model_errors=True,
            required_packages=())

    def run(self, job, context):
        with isolated_agent(self.settings, context.env) as env:
            return self._run(job, context.model_copy(update={"env": env}))

    def _run(self, job, context):
        workspace = Path(context.workspace_path)
        scope = job.spec.parameters["scope"]
        context = context.model_copy(update={"env": {**context.env,
            "DATAELF_SERVER_TRANSPORT": self.settings.server.pi.transport}})
        first = self._runner(scope).run(job, context)
        check_cancelled()
        issues = self._issues(workspace, job)
        # Only parser/termination false negatives can recover validated output.
        recoverable = (first.error_code or "").split(":")[0] in {"PI_EVENT_PARSE_ERROR", "PI_MODEL_ERROR"}
        if not issues and (first.status == "completed" or recoverable):
            self._record(workspace, "pi", first, issues)
            return ExplorerRunResult(status="completed", artifacts=first.artifacts + self._inventory(workspace))
        self._record(workspace, "pi", first, issues)
        verified = not validate_analysis_manifest(workspace) if scope == "scope_v2" else self._legacy_evidence(workspace)
        retryable = first.status == "completed" or (first.error_code or "").split(":")[0] in {
            "PI_PROCESS_TIMEOUT", "PI_EVENT_PARSE_ERROR", "PI_PROCESS_NONZERO_EXIT", "PI_MODEL_ERROR"}
        if not verified or not retryable:
            return self._failure(first, issues, workspace)
        prompt = workspace / "prompts/synthesis_retry.md"
        prompt.write_text(_build_synthesis_retry_prompt(job, workspace, format_issues=issues, ontology_mode=scope == "scope_v2"), encoding="utf-8")
        retry_context = context.model_copy(update={"prompt_path": str(prompt), "env": {**context.env, "DATAELF_PI_SYNTHESIS_ONLY": "1"}})
        second = self._runner(scope, retry=True).run(job, retry_context)
        check_cancelled()
        issues = self._issues(workspace, job)
        self._record(workspace, "pi_synthesis_retry", second, issues)
        artifacts = first.artifacts + second.artifacts + self._inventory(workspace)
        if not issues and (second.status == "completed" or (second.error_code or "") in {"PI_EVENT_PARSE_ERROR", "PI_MODEL_ERROR"}):
            return ExplorerRunResult(status="completed", artifacts=artifacts)
        failed = self._failure(second, issues, workspace)
        return failed.model_copy(update={"artifacts": artifacts})

    def _issues(self, workspace, job):
        try:
            validate_outputs(workspace, ServerProfile(self.settings).output_contract(job.spec))
            workspace_insights(workspace, scope=job.spec.parameters["scope"])
            return []
        except (PipelineError, ArtifactContractError, OSError, ValueError) as exc:
            return [str(exc)]

    @staticmethod
    def _legacy_evidence(workspace):
        try:
            value = json.loads((workspace / "insights/candidate_signals.json").read_text())
            return bool(value.get("candidate_signals")) and any(workspace.glob("deep_dives/*.md"))
        except (OSError, ValueError):
            return False

    @staticmethod
    def _record(workspace, prefix, result, issues):
        (workspace / f"logs/{prefix}_completion.json").write_text(json.dumps({
            "status": result.status, "error_code": result.error_code, "validation_issues": issues,
            "formally_valid": not issues}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _failure(self, result, issues, workspace):
        return ExplorerRunResult(status="failed", artifacts=result.artifacts + self._inventory(workspace),
            error_code=result.error_code or "insight_format_invalid", error_message=result.error_message or "; ".join(issues))

    @staticmethod
    def _inventory(workspace):
        paths = [p for folder in ("scripts", "tables", "notes", "deep_dives") for p in sorted((workspace / folder).rglob("*")) if p.is_file()]
        paths += sorted(workspace.glob("logs/*completion.json")) + sorted(workspace.glob("logs/pi_*manifest.json")) + sorted(workspace.glob("prompts/synthesis_retry.md"))
        return [ArtifactRef(artifact_id=f"server_analysis_{n}", kind="analysis_evidence", path=p.relative_to(workspace).as_posix(), role="evidence", producer_stage="explorer") for n, p in enumerate(paths)]
