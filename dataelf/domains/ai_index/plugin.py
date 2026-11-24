from __future__ import annotations

import json
import re
from pathlib import Path

from dataelf.config import DataElfConfig
from dataelf.discovery.policies import skill_review
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
from dataelf.domains.ai_index.modeler import AIIndexModeler
from dataelf.domains.ai_index.prompt import build_ai_index_prompt
from dataelf.domains.ai_index.review import review_ai_index, insight_output_schema
from dataelf.domains.ai_index.table_builder import ensure_table_schemas


class AIIndexDomainPlugin:
    def __init__(self, config: DataElfConfig, manifest: DomainManifest):
        self.manifest = manifest
        self.config = AIIndexDomainConfig.from_mapping(config.domain_config(manifest.domain))
        self.config.source.validate_for_run()
        self.runtime_env = dict(config.env)
        self.runtime_config = config

    owns_agent_resources = True

    def agent_resources(self, spec, config):
        from dataelf.discovery.contracts import AgentResources
        from dataelf.discovery.agent_resources import discover_domain_resources
        from dataelf.discovery.policies import skill_review
        if not skill_review(spec):
            return AgentResources()
        return discover_domain_resources(Path(__file__).parent)

    def normalize_spec(self, spec: JobSpec) -> JobSpec:
        query = spec.objective
        parameters = dict(spec.parameters)
        from dataelf.discovery.policies import skill_review
        if skill_review(spec):
            self.config.modeling = self.config.modeling.model_copy(update=self.runtime_config.ontology_for_domain(self.manifest.domain).model_dump())
            self.config.validate_for_run()
            parameters.setdefault('expected_outputs',_extract_expected_outputs(query))
            if parameters['expected_outputs'] is not None and (type(parameters['expected_outputs']) is not int or parameters['expected_outputs']<1):
                raise ValueError('expected_outputs must be a positive integer or null')
            parameters.setdefault('need_web_search',False)
            return spec.model_copy(update={'parameters':parameters,
                'requested_outputs':spec.requested_outputs or ['candidate_signals','insight_candidates','final_brief'],
                'modeling_strategy':spec.modeling_strategy or ('ontology_rdf' if self.config.modeling.enabled else None)})
        from .legacy_entry import normalize_spec as legacy_normalize
        return legacy_normalize(self, spec)

    def prepare(self, spec: JobSpec, workspace_path: str, config: DataElfConfig) -> StageResult:
        workspace = Path(workspace_path)
        for relative in self.manifest.workspace_dirs:
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        from dataelf.discovery.policies import skill_review
        if skill_review(spec):
            if self.config.source.mode!='api':raise ValueError('AI Index Skill workflow requires API mode; fixtures are not a live fallback')
        else:ensure_table_schemas(workspace)
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
        from dataelf.discovery.policies import skill_review
        if not skill_review(spec) and (spec.modeling_strategy is not None or self.config.modeling.enabled):
            raise ValueError("Legacy CLI modeling configuration requires explicit migration: select --workflow-policy cli_skill_review_v1 for shared ontology v2")
        if spec.modeling_strategy is None:
            return None
        if spec.modeling_strategy != "ontology_rdf":
            raise ValueError(f"Unsupported AI Index modeling strategy: {spec.modeling_strategy}")
        if not self.config.modeling.enabled:
            raise ValueError("ontology_rdf modeling requires ontology.enabled=true")
        return AIIndexModeler(self.config, self.runtime_env, self.runtime_config)

    def build_prompt(self, job: DiscoveryJob, context: DiscoveryContext) -> str:
        from dataelf.discovery.policies import skill_review
        if skill_review(job.spec):
            return "围绕用户的 AI 科技研究任务，使用本 domain 的数据源 Skill 调研。区分来源事实、分析推断与未验证问题；最终报告回答用户问题，审核记录另存。"
        from .legacy_prompt import build_ai_index_prompt as legacy_prompt
        return legacy_prompt(job, context)

    def output_contract(self, spec: JobSpec) -> OutputContract:
        return OutputContract(
            contract_id="ai_index.insight_discovery",
            version="2" if skill_review(spec) else "1",
            artifacts=[
                OutputArtifactSpec(
                    artifact_id="candidate_signals", path="insights/candidate_signals.json",
                    kind="candidate_signals", media_type="application/json", json_root="candidate_signals",
                ),
                OutputArtifactSpec(
                    artifact_id="insight_candidates", path="insights/insight_candidates.json",
                    kind="insight_candidates", media_type="application/json", json_root="insight_candidates",
                    json_schema=insight_output_schema(new_submission=True) if skill_review(spec) else None,
                ),
                OutputArtifactSpec(
                    artifact_id="final_brief", path="insights/final_brief.md",
                    kind="final_brief", media_type="text/markdown",
                ),
            ],
        )

    def review(self, job: DiscoveryJob, workspace_path: str) -> ReviewResult:
        from dataelf.discovery.policies import skill_review
        if not skill_review(job.spec):
            from .legacy_review import review_ai_index as legacy_review
            return legacy_review(job, Path(workspace_path))
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


def _extract_expected_outputs(query: str) -> int | None:
    """Recognize output counts, never counts embedded in entity descriptions."""
    match = re.search(r"(?<![同第])(\d+|[一二两三四五六七八九十])\s*[个条项]\s*(?:发现|洞察|结论|insights?)", query, re.I)
    if not match or '至少' in query[max(0, match.start() - 6):match.start()]:
        return None
    token = match.group(1)
    if token.isdigit():
        return max(1, int(token))
    return {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[token]


__all__ = ["AIIndexDomainPlugin", "create_plugin"]
