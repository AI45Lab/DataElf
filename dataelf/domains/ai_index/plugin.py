from __future__ import annotations

import json
import re
from pathlib import Path

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import (
    DiscoveryContext,
    DiscoveryJob,
    DomainManifest,
    JobSpec,
    OutputArtifactSpec,
    OutputContract,
    ReviewResult,
    StageResult,
)
from dataelf.domains.ai_index.config import AIIndexDomainConfig
from dataelf.domains.ai_index.modeling import AIIndexModeler
from dataelf.domains.ai_index.prompt import build_ai_index_prompt
from dataelf.domains.ai_index.review import review_ai_index
from dataelf.domains.ai_index.table_builder import ensure_table_schemas


class AIIndexDomainPlugin:
    def __init__(self, config: DataElfConfig, manifest: DomainManifest):
        self.manifest = manifest
        self.config = AIIndexDomainConfig.from_mapping(config.domain_config(manifest.domain))
        self.runtime_env = dict(config.env)

    def normalize_spec(self, spec: JobSpec) -> JobSpec:
        query = spec.objective
        parameters = dict(spec.parameters)
        parameters.setdefault("topic", _extract_topic(query))
        parameters.setdefault("domains", ["LLMs"])
        parameters.setdefault("sub_domains", ["Agentic LLMs"])
        parameters.setdefault("time_window", "last_6_months")
        parameters.setdefault("expected_outputs", _extract_expected_outputs(query))
        parameters.setdefault("need_web_search", "联网" in query or "web" in query.lower() or "search" in query.lower())
        requested_outputs = spec.requested_outputs or ["candidate_signals", "insight_candidates", "final_brief"]
        strategy = spec.modeling_strategy
        if strategy is None and self.config.modeling.enabled:
            strategy = "ontology_rdf"
        return spec.model_copy(update={
            "parameters": parameters,
            "requested_outputs": requested_outputs,
            "modeling_strategy": strategy,
        })

    def prepare(self, spec: JobSpec, workspace_path: str, config: DataElfConfig) -> StageResult:
        workspace = Path(workspace_path)
        for relative in self.manifest.workspace_dirs:
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        ensure_table_schemas(workspace)
        source = self.config.source
        return StageResult(
            status="completed",
            context={"source_mode": source.mode},
            env={
                "DATAELF_AI_INDEX_MODE": source.mode,
                "AI_INDEX_BASE_URL": source.base_url,
                "AI_INDEX_API_KEY": source.api_key,
                "DATAELF_FIXTURES_DIR": str(source.fixtures_dir),
            },
        )

    def create_modeler(self, spec: JobSpec, config: DataElfConfig) -> AIIndexModeler | None:
        if spec.modeling_strategy is None:
            return None
        if spec.modeling_strategy != "ontology_rdf":
            raise ValueError(f"Unsupported AI Index modeling strategy: {spec.modeling_strategy}")
        if not self.config.modeling.enabled:
            raise ValueError("ontology_rdf modeling requires domains.ai_index.modeling.enabled=true")
        return AIIndexModeler(self.config, self.runtime_env)

    def build_prompt(self, job: DiscoveryJob, context: DiscoveryContext) -> str:
        return build_ai_index_prompt(job, context)

    def output_contract(self, spec: JobSpec) -> OutputContract:
        return OutputContract(
            contract_id="ai_index.insight_discovery",
            version="1",
            artifacts=[
                OutputArtifactSpec(
                    artifact_id="candidate_signals", path="insights/candidate_signals.json",
                    kind="candidate_signals", media_type="application/json", json_root="candidate_signals",
                ),
                OutputArtifactSpec(
                    artifact_id="insight_candidates", path="insights/insight_candidates.json",
                    kind="insight_candidates", media_type="application/json", json_root="insight_candidates",
                ),
                OutputArtifactSpec(
                    artifact_id="final_brief", path="insights/final_brief.md",
                    kind="final_brief", media_type="text/markdown",
                ),
            ],
        )

    def review(self, job: DiscoveryJob, workspace_path: str) -> ReviewResult:
        return review_ai_index(job, Path(workspace_path))

    def result_ids(self, workspace_path: str) -> list[str]:
        path = Path(workspace_path) / "insights" / "insight_candidates.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = payload.get("insight_candidates", []) if isinstance(payload, dict) else []
        return [str(item["insight_id"]) for item in items if isinstance(item, dict) and item.get("insight_id")]


def create_plugin(config: DataElfConfig, manifest: DomainManifest) -> AIIndexDomainPlugin:
    return AIIndexDomainPlugin(config, manifest)


def _extract_topic(query: str) -> str:
    match = re.search(r"围绕\s*([^，,]+)", query)
    if match:
        return match.group(1).strip()
    return "Agentic LLMs" if "agent" in query.lower() or "智能体" in query else "AI science intelligence"


def _extract_expected_outputs(query: str) -> int:
    match = re.search(r"(\d+)\s*个", query)
    if match:
        return max(1, min(int(match.group(1)), 5))
    for token, value in {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}.items():
        if f"{token}个" in query:
            return value
    return 3


__all__ = ["AIIndexDomainPlugin", "create_plugin"]
