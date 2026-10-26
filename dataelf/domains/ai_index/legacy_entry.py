"""AI Index legacy task normalization, from 78f2ef4."""
import re
from dataelf.discovery.contracts import JobSpec

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
