"""HTTP task adapter; all research stages are orchestrated by core run_job."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Event, RLock

from dataelf.discovery.artifacts import validate_outputs
from dataelf.discovery.contracts import ArtifactRef, DiscoveryJob, JobSpec
from dataelf.discovery.run_control import RunControl
from dataelf.discovery.workflow import NullStore, _finalize, _write_review, run_job
from dataelf_server.analysis.validation import PipelineError, workspace_insights
from dataelf_server.workflows.profile import ServerProfile
from dataelf_server.workflows.explorer import ServerExplorer

STAGES = {
    "intent_recognition": ("recognizing_intent", 10),
    "initialization": ("preparing_workspace", 5),
    "domain_prepare": ("fetching_ai_index", 15),
    "domain_prepared": ("source_ready", 30),
    "domain_modeling": ("analyzing_with_pi", 40),
    "prompt_composition": ("analyzing_with_pi", 40),
    "explorer": ("analyzing_with_pi", 40),
    "output_validation": ("validating_insights", 90),
    "domain_review": ("validating_insights", 90),
    "finalization": ("validating_insights", 95),
}


class ServerPipeline:
    def __init__(self, settings, *, profile_factory=ServerProfile, explorer_factory=ServerExplorer):
        self.settings = settings
        self.profile_factory = profile_factory
        self.explorer_factory = explorer_factory
        self._lock = RLock()
        self._closed = Event()
        self._controls: dict[int, RunControl] = {}

    def cancel(self):
        self._closed.set()
        with self._lock:
            controls = list(self._controls.values())
        for control in controls:
            control.cancel()

    def run(self, *, job_id, request_payload, workspace_path, progress, source_trace):
        workspace = workspace_path.resolve()
        def stage_event(stage):
            progress(*STAGES[stage])
            if stage == "domain_prepared":
                source_trace(_source_trace(workspace))
        control = RunControl(job_id=job_id, workspace_path=workspace, on_stage=stage_event)
        with self._lock:
            self._controls[id(control)] = control
            if self._closed.is_set():
                control.cancel()
        try:
            spec = JobSpec(domain="ai_index", objective=request_payload["query"],
                parameters={"scope": request_payload.get("scope", "scope_v2")}, workflow_profile="server")
            profile = self.profile_factory(self.settings)
            job = run_job(spec, self.settings.execution_config(), plugin=profile,
                explorer=self.explorer_factory(self.settings), control=control)
            source_trace(_source_trace(workspace))
            if job.status != "completed":
                raise PipelineError(job.error_code or "internal_error", job.error_message or "Job failed")
            return workspace_insights(workspace, scope=spec.parameters["scope"])
        finally:
            with self._lock:
                self._controls.pop(id(control), None)


def _source_trace(workspace):
    trace = None
    for path in sorted(workspace.glob("scope_v2/*/result.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for block in document.get("sources", {}).values():
            values = block.get("trace_ids", [])
            if values:
                trace = values[-1]
    for path in sorted(workspace.glob("scope_v2/*/error.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        trace = document.get("error", {}).get("trace_id") or trace
    for path in sorted(workspace.glob("raw/ai_index/*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        trace = document.get("trace_id") or trace
    return trace


def recover_workspace_insights(job_id: str, workspace_path: Path, settings):
    """Validate existing artifacts and synchronize core finalization, without execution."""
    spec = JobSpec.model_validate_json((workspace_path / "job_spec.json").read_text())
    profile = ServerProfile(settings)
    insights = workspace_insights(workspace_path, scope=spec.parameters["scope"])
    outputs, warnings = validate_outputs(workspace_path, profile.output_contract(spec))
    manifest = json.loads((workspace_path / "artifact_manifest.json").read_text())
    refs = [ArtifactRef.model_validate(item) for item in manifest["artifacts"]]
    refs = list({ref.artifact_id: ref for ref in [*refs, *outputs]}.values())
    job = DiscoveryJob(job_id=job_id, spec=spec, workspace_path=str(workspace_path), artifacts=refs)
    review = profile.review(job, str(workspace_path))
    if review.status == "failed":
        raise PipelineError("quality_review_failed", "; ".join(review.warnings))
    review.warnings.extend(warnings)
    _write_review(workspace_path, review)
    _finalize(job, NullStore(), workspace_path, profile, review)
    return insights
