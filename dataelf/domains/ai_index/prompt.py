from __future__ import annotations

from dataelf.discovery.contracts import DiscoveryContext, DiscoveryJob


def build_ai_index_prompt(job: DiscoveryJob, context: DiscoveryContext) -> str:
    modeled = any(artifact.kind == "ontology_rdf" for artifact in context.artifacts)
    source = _rdf_source() if modeled else _table_source()
    return f"""You are DataElf's AI technology-intelligence analyst. Discover non-obvious, evidence-backed insights; do not merely summarize fields or produce top-N rankings.

## Evidence source

{source}

You may use external web search to explain or challenge an AI Index signal. Save external observations under `raw/web/`, `tables/source_observations.csv`, or `tables/external_findings.csv`. Never fabricate external facts; state clearly when web search is unavailable.

## Required method

1. Breadth scan: generate 8-12 varied candidate signals and write `insights/candidate_signals.json`.
2. Selection: score novelty, magnitude, relation complexity, strategic relevance, actionability, low-base risk, and obviousness risk; select at most 3.
3. Deep dive: write and execute reusable Python analysis under `scripts/`, save derived evidence under `tables/`, and write a non-empty report under `deep_dives/` for each selected signal.
4. Synthesis: write `insights/insight_candidates.json` and `insights/final_brief.md`.

Keep the run bounded. Prefer one compact analysis script per phase and stop acquiring data once the selected insights have enough evidence.

## Insight standard

- Cover at least two insight forms when evidence permits: mechanism, structural relationship, anomaly, opportunity/risk, contradiction, ecosystem gap, or timing.
- Connect at least two entity or evidence types in each final insight.
- At least two insights should cite executed Python analysis artifacts when enough evidence exists.
- Check counterarguments, alternative explanations, low-base effects, provenance quality, and uncertainty.
- Produce fewer strong insights rather than filling a quota with weak claims.

Each item in `insight_candidates` must include: `insight_id`, `title`, `thesis`, `why_now`, `supporting_signals`, `analysis_artifacts`, `related_entities`, `external_support`, `counterarguments`, `confidence`, and `next_questions`. Confidence must be numeric from 0 to 1.

User objective: {job.spec.objective}
"""


def _table_source() -> str:
    return """Use normalized CSV tables under `tables/` for quantitative analysis and `raw/ai_index/` only for original details or provenance. When more data is needed, write a script under `scripts/` and use:

```python
from dataelf.domains.ai_index.client import AIIndexClient
client = AIIndexClient.from_env()
```

Available methods are `search_papers`, `search_institutions`, `search_scholars`, and `fetch_institution_funding`. They persist raw responses and update tables. Do not call the AI Index HTTP API directly."""


def _rdf_source() -> str:
    return """An AI Index ontology/RDF modeling stage has completed. Treat the `ontology_rdf` artifact in the prepared artifact inventory as the primary factual source. Use its named graphs deliberately: domain entities, observations, source provenance, and schema. Use raw files only to follow provenance. Do not rebuild the old CSV-first analysis path; derived SPARQL result CSVs are allowed. Write a consolidated RDFLib/SPARQL analysis script early in the run and trace selected claims back through provenance."""


__all__ = ["build_ai_index_prompt"]
