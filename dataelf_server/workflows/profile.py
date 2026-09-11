from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.contracts import ArtifactRef, DomainManifest, OutputArtifactSpec, OutputContract, ReviewResult, StageResult
from dataelf.schemas import new_id
from dataelf.discovery.run_control import RunCancelled, check_cancelled, current_run
from dataelf_server.analysis.insight_contract import write_contract, load_contract_text
from dataelf_server.analysis.review import review_workspace
from dataelf_server.analysis.validation import PipelineError, workspace_insights
from dataelf_server.scope_v2 import prefetch_scope_v2, materialize_filtered_ai_index_envelopes
from dataelf_server.settings import Settings
from dataelf_server.intent import IntentRecognizer
from dataelf_server.intent.schema import Output
from dataelf_server.analysis.writing import TASK_OBJECTIVE
from dataelf_server.intent.planner import build_scope_plan
from dataelf_server.scope_v2.contracts import ScopePlan, ScopeCall, TimeWindow, TIMEZONE
from datetime import datetime
from dataelf_server.workflows.modeler import ServerModeler


class ServerProfile:
    """Server-owned domain behavior plugged into the common job workflow."""
    name = "server"
    manifest = DomainManifest(domain="ai_index", version="1", display_name="AI Index Server", plugin="dataelf_server.workflows.profile:ServerProfile", workspace_dirs=["insights", "deep_dives", "scope_v2", "modeling/server"])

    def __init__(self, settings: Settings, *, prefetcher=None, recognizer=None):
        self.settings = settings
        self.prefetcher = prefetcher
        self.recognizer = recognizer
        self.domain = settings.domain

    def normalize_spec(self, spec):
        parameters = dict(spec.parameters)
        scope = parameters.setdefault("scope", "scope_v2")
        if scope != "scope_v2":
            raise PipelineError("invalid_scope", "The archived rule-based scope is disabled")
        control = current_run()
        if control:
            control.emit("intent_recognition")
        check_cancelled()
        reference_time = datetime.now(TIMEZONE)
        recognizer = self.recognizer or IntentRecognizer(config=self.settings.intent_config)
        intent = recognizer.extract(spec.objective, reference_time=reference_time)
        check_cancelled()
        if control and control.workspace_path:
            audit_dir = Path(control.workspace_path) / "logs"
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / "request_input.json").write_text(json.dumps({
                "query": spec.objective, "intent": intent.model_dump(),
                "reference_time": reference_time.isoformat(), "purpose": "audit_only",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Neither the effective JobSpec nor any downstream Scope plan/result embeds
        # the original request. Only the extracted fields are executable inputs.
        plan = build_scope_plan(intent, TASK_OBJECTIVE, now=reference_time,
                                max_pages=self.settings.server.source.max_pages)
        parameters["scope_plan"] = plan.to_dict()
        parameters["intent_input"] = intent.model_dump(exclude={"output"})
        parameters["output"] = intent.output.model_dump()
        return spec.model_copy(update={"objective": TASK_OBJECTIVE, "parameters": parameters, "modeling_strategy": "scope_v2_fixed",
            "requested_outputs": ["candidate_signals", "insight_candidates", "final_brief"]})

    def prepare(self, spec, workspace_path, config):
        workspace = Path(workspace_path)
        for relative in self.manifest.workspace_dirs:
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        self.domain.source.validate_for_run()
        scope = spec.parameters["scope"]
        plan_data = spec.parameters.get("scope_plan", {})
        contract = write_contract(workspace, mode=plan_data["mode"], modules=plan_data.get("modules", []),
                                  output=Output.model_validate(spec.parameters["output"]),
                                  retrieval=spec.parameters["intent_input"]["retrieval"])
        env = {"DATAELF_SCOPE": scope, "DATAELF_INSIGHT_CONTRACT_PATH": str(contract), "DATAELF_WORKFLOW_PROFILE": "server"}
        artifacts = [ArtifactRef(artifact_id="server_writing_contract", kind="writing_contract", path=contract.relative_to(workspace).as_posix(), role="input", producer_stage="domain_prepare", media_type="text/markdown")]
        context = {"scope": scope}
        if self.prefetcher is None and self.domain.source.mode != "api":
            raise PipelineError("SERVER_CONFIGURATION_INVALID", "Scope V2 requires domains.ai_index.source.mode=api")
        plan = ScopePlan(**{**plan_data, "window": TimeWindow(**plan_data["window"]), "calls": [ScopeCall(**call) for call in plan_data["calls"]]})
        try:
            result = self.prefetcher(plan, workspace) if self.prefetcher else prefetch_scope_v2(plan, workspace, base_url=self.domain.source.base_url, api_key=self.domain.source.api_key)
            materialize_filtered_ai_index_envelopes(plan, result, workspace)
        except RunCancelled:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "acquisition_failed")
            if not code.startswith("ai_index_") and code != "no_source_data":
                code = "ai_index_" + code
            raise PipelineError(code, str(exc)) from exc
        for index, path in enumerate(sorted([*workspace.glob("scope_v2/**/*"), *workspace.glob("raw/ai_index/*.json")])):
            if path.is_file():
                artifacts.append(ArtifactRef(artifact_id=f"server_source_{index}", kind="source_data", path=path.relative_to(workspace).as_posix(), role="input", producer_stage="domain_prepare", media_type="application/json"))
        context.update(scope_v2_result_path=str(result.result_path), source_trace_id=result.source_trace_id)
        env["DATAELF_SCOPE_V2_RESULT"] = str(result.result_path)
        return StageResult(status="completed", artifacts=artifacts, context=context, env=env)

    def create_modeler(self, spec, config):
        return ServerModeler()

    def build_prompt(self, job, context):
        path = Path(__file__).resolve().parents[1] / "prompts" / "research.md"
        text = path.read_text(encoding="utf-8")
        text += "\nUse dataelf_submit_rdf_analysis to submit your complete analysis program, then dataelf_finalize_rdf_insights to produce final outputs. Read the explicit RDF artifact and the prefetched Scope V2 result. Do not make additional source API or web requests.\n"
        text += (path.parent / "scope_v2_inputs.md").read_text(encoding="utf-8")
        return text + "\n" + load_contract_text(Path(context.workspace_path))

    def output_contract(self, spec):
        return OutputContract(contract_id="server.insight_discovery", artifacts=[
            OutputArtifactSpec(artifact_id="candidate_signals", path="insights/candidate_signals.json", kind="candidate_signals", media_type="application/json", json_root="candidate_signals"),
            OutputArtifactSpec(artifact_id="insight_candidates", path="insights/insight_candidates.json", kind="insight_candidates", media_type="application/json", json_root="insight_candidates"),
            OutputArtifactSpec(artifact_id="final_brief", path="insights/final_brief.md", kind="final_brief", media_type="text/markdown"),
        ])

    def review(self, job, workspace_path):
        try:
            insights = workspace_insights(Path(workspace_path), scope=job.spec.parameters["scope"])
            return review_workspace(job.job_id, Path(workspace_path), scope=job.spec.parameters["scope"])
        except (PipelineError, OSError, ValueError) as exc:
            return ReviewResult(review_id=new_id("review"), job_id=job.job_id, status="failed", warnings=[str(exc)])

    def result_ids(self, workspace_path):
        try:
            data = json.loads((Path(workspace_path) / "insights/insight_candidates.json").read_text())
            return [str(item["insight_id"]) for item in data["insight_candidates"]]
        except (OSError, ValueError, KeyError, TypeError):
            return []
