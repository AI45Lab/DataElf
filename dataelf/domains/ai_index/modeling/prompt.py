from __future__ import annotations

import json
import sys
from pathlib import Path

from dataelf.discovery.base import DiscoveryContext
from dataelf.domains.ai_index.modeling.contracts import OntologyRunResult
from dataelf.schemas import DiscoveryJob


def write_ai_index_modeling_prompt(
    job: DiscoveryJob,
    context: DiscoveryContext,
    result: OntologyRunResult,
) -> Path:
    workspace = Path(context.workspace_path)
    prompt_path = workspace / "prompts" / "discovery_prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(build_ai_index_modeling_prompt(job, context, result), encoding="utf-8")
    return prompt_path


def build_ai_index_modeling_prompt(
    job: DiscoveryJob,
    context: DiscoveryContext,
    result: OntologyRunResult,
) -> str:
    workspace = Path(context.workspace_path).resolve()
    run = result.model_dump(mode="json")
    nquads = Path(str(run.get("nquads_path", ""))).resolve()
    rdfxml = Path(str(run.get("rdfxml_path", ""))).resolve()
    ntriples = Path(str(run.get("ntriples_path", ""))).resolve()
    manifest = Path(str(run.get("manifest_path", ""))).resolve()
    validation = Path(str(run.get("validation_path", ""))).resolve()
    stage1_bundle = Path(str(run.get("stage1_bundle", ""))).resolve()
    stage2_bundle = Path(str(run.get("stage2_bundle", ""))).resolve()
    ontology = stage1_bundle / "ontology.json"
    grounding = stage1_bundle / "grounding.json"
    review = stage2_bundle / "review.json"
    metrics = stage2_bundle / "metrics.json"
    projection_lineage = stage2_bundle / "projection_lineage.json"
    seed_query = job.seed_query or ""
    scope_json = json.dumps(job.scope, ensure_ascii=False, indent=2)
    constraints_json = json.dumps(job.constraints, ensure_ascii=False, indent=2)
    model_line = f"\nPreferred model: `{context.model}`\n" if context.model else ""
    python_runtime = Path(sys.executable).resolve()
    return f"""# DataElf RDF Insight Discovery Task

You are DataElf DiscoveryAgent. Discover non-obvious, evidence-backed technology intelligence from a reviewed AI Index RDF graph.
{model_line}
## Workspace and authoritative inputs

Write every new artifact under this job workspace:

`{workspace}`

The authoritative AI Index analysis inputs are:

- Canonical N-Quads dataset: `{nquads}`
- N-Triples graph: `{ntriples}`
- Stable RDF/XML graph: `{rdfxml}`
- Stage 2 manifest: `{manifest}`
- Stage 2 validation: `{validation}`
- Stage 2 independent review: `{review}`
- Stage 2 metrics: `{metrics}`
- Projection lineage: `{projection_lineage}`
- Reviewed ontology: `{ontology}`
- Reviewed grounding contract: `{grounding}`

Use `graph.nq` as the primary factual source because it preserves the four named graphs. Treat `graph.nt` and `graph.rdf` only as union compatibility views. Read the ontology and grounding contract to understand classes, properties, provenance, observations, and raw lineage.

Query the named graphs deliberately:

- `urn:dataelf:ontology:ai-index:graph/domain` for stable domain entities and projected relationships.
- `urn:dataelf:ontology:ai-index:graph/observation` for ranks, citations, hotness, funding, and endpoint-specific snapshots.
- `urn:dataelf:ontology:ai-index:graph/source` only for provenance verification and exact raw replay; never mix its fragment triples into analytical counts.
- `urn:dataelf:ontology:ai-index:graph/schema` for class/property discovery when the reviewed ontology is insufficient.

Do not use the job's `tables/*.csv` as the primary AI Index source and do not rebuild the old CSV-first workflow. Derived query-result CSV files are allowed. Inspect `raw/ai_index/` only when following provenance or resolving a detail absent from the graph.

## Analysis method

1. Use at most the first three tool calls to inspect the ontology, manifest, validation metrics, graph namespaces, named-graph counts, classes, and properties. Do not inspect raw files during this bounded orientation phase.
2. By the fourth tool call, write one self-contained `scripts/analyze_rdf.py` using RDFLib and SPARQL or graph traversal, then execute it with `{python_runtime}`. The script must generate the query tables, notes, candidate signals, selected insights, deep dives, and final brief in one bounded pass. Do not use system `python3`, invoke `pip`, or install packages during discovery.
3. Save reusable SPARQL queries as `scripts/*.rq` when useful.
4. Save compact derived query results under `tables/` and interpretation notes under `notes/`.
5. Generate 8-12 candidate signals in `insights/candidate_signals.json`.
6. Select up to 3 high-value signals and write a non-empty `deep_dives/*.md` report for each selected signal.
7. For each selected claim, trace at least one path from domain entity through observation/source record to the raw file and JSON Pointer.
8. Challenge mechanisms, provenance quality, low-base effects, alternative explanations, and uncertainty.
9. Produce the final structured artifacts, verify them, and stop.

This is a hard convergence budget: do not spend more than three calls exploring, and aim to finish within eight total tool calls. Once orientation is complete, the next call must create and run the consolidated analysis script rather than reading more inputs. Do not print the whole RDF graph, raw JSON, or large query results to stdout.

External web evidence may be saved to `raw/web/`, `tables/source_observations.csv`, and `tables/external_findings.csv`. If web search is unavailable, state that limitation rather than fabricating support.

## Insight requirements

- Do not return generic summaries, field restatements, or simple top-N rankings.
- Prefer mechanism, structural relationship, anomaly, opportunity/risk, contradiction, ecosystem gap, or timing insights.
- Every final insight must cite RDF/SPARQL analysis artifacts and connect at least two entity types.
- At least two final insights should be backed by executed RDF analysis scripts when enough evidence exists.
- Preserve exact artifact paths relative to the workspace in `analysis_artifacts`.

## Required outputs

Write `insights/candidate_signals.json`:

```json
{{
  "candidate_signals": [
    {{
      "signal_id": "sig_001",
      "signal_type": "structural_relationship",
      "summary": "...",
      "why_might_matter": "...",
      "supporting_tables": ["rdf_query_result.csv"],
      "related_entities": ["Paper", "Institution"],
      "suggested_deep_dive": ["..."],
      "initial_score": {{"novelty": 0.0, "magnitude": 0.0, "strategic_relevance": 0.0}},
      "status": "needs_deep_dive"
    }}
  ]
}}
```

Write `insights/insight_candidates.json`:

```json
{{
  "insight_candidates": [
    {{
      "insight_id": "ins_001",
      "title": "...",
      "thesis": "...",
      "why_now": "...",
      "supporting_signals": ["sig_001"],
      "analysis_artifacts": ["scripts/query.py", "scripts/query.rq", "tables/rdf_query_result.csv", "deep_dives/sig_001.md"],
      "related_entities": ["Paper:...", "Institution:..."],
      "external_support": [],
      "counterarguments": ["..."],
      "confidence": 0.0,
      "next_questions": ["..."]
    }}
  ]
}}
```

Also write `insights/final_brief.md`. Before stopping, verify both JSON files, the brief, at least one RDF analysis script, and at least one deep-dive report are non-empty.

## User task

`{seed_query}`

Scope:

```json
{scope_json}
```

Constraints:

```json
{constraints_json}
```
"""


__all__ = ["build_ai_index_modeling_prompt", "write_ai_index_modeling_prompt"]
