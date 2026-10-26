# Ontology and shared RDF graph service

DataElf uses **one autonomous modeler and one independent reviewer**. Modeling,
review and analysis use the same `GraphService`. Business vocabulary and identity
choices remain in executable contracts and evidence-backed agent decisions.
RDFLib remains the backend; no graph database, general OWL reasoner or new
expression language is required.

## Configuration and migration

The outer configuration contains `ontology.enabled`, `ontology.ontology_config`
and the trusted `ontology.maintenance_review_policy` (`required` by default).
Relative template paths resolve against that file.
The absent default file falls back to `config.yaml.example`; explicit custom
paths must exist. Disabled ontology does not load models or templates.

```yaml
mode: dynamic
templates: []
save_as_template: false
max_repairs: 5
max_control_restarts: 3
max_llm_requests: 160
max_llm_tokens: null
timeout_seconds: 9120
output_subdir: ontology
modeler:
  model: ""
  tool_choice: auto
  timeout_seconds: 1800
  max_requests: 60
  max_output_tokens: 8192
  stream_usage: true
reviewer:
  model: ""
  tool_choice: auto
  timeout_seconds: 1800
  max_output_tokens: 8192
  stream_usage: true
```

Omitted role models inherit `explorer.pi.model`. Provider registration and
credentials continue to belong to the main Pi configuration. Ontology disables
thinking for its roles, including both GLM/ZAI gateway switches.
The example shows an optional output limit; configure it for your own service. `stream_usage: true`
requests OpenAI-compatible streaming usage even when an older Pi registry disables
it; enable it only for a gateway that supports `stream_options.include_usage`.
Analysis uses `env.DATAELF_PI_STREAM_USAGE: "1"` in the main configuration for
the same explicit capability. An explicit role setting takes precedence.

Legacy `stage1.generator` maps to `modeler`; `stage2.reviewer`, when configured,
otherwise `stage1.reviewer`, maps to the single `reviewer`.
`stage1.save_as_template` maps to `save_as_template`. `stage2.generator` is retired;
the loader emits a deprecation warning. The deprecated `named` step router maps
explicitly to `auto` with a warning. Optional provider `required` is supported,
but the normal workflow uses `auto`. The former narrative/no-progress fields
remain readable and have no default hard interception.
`ExtractionPlan` remains a compatibility model; it does not drive another agent.
Selectors are part of the modeler's final contract.

Historical `domains.ai_index.modeling` configuration still follows the existing
one-way migration warnings. The deleted private modeling package is not restored.
AI Index CLI now accepts dynamic modeling or explicit continuation via
`--resume-graph-manifest`; its former template execution/export product entry is
retired. Versioned executable contracts remain available. Shared template APIs
and other domains retain their existing behavior until their owners migrate.
Serve composes its unchanged approved Scope V2 fragments from
`dataelf_server/ontology/templates/scope_v2` and executes the complete contract
without modeling model calls.

## Autonomous modeling

```python
from pathlib import Path
from dataelf.ontology import OntologyRequest, SourceInput, run_ontology
result = run_ontology(OntologyRequest(
    domain="retail", workspace=Path("/tmp/job"),
    sources=[SourceInput(source_id="orders", path=Path("raw/orders.json"))],
    objective="Model orders and customers using source identifiers.",
))
```

Data collection stays in the domain. JSON, JSONL and CSV snapshots and supplied
documents must be inside the workspace. Source hashes are checked throughout the
run and again at commit. Existing profiling, projection, exact replay, recursive
array mappings, expression operators, identities and conflict policies remain.

The modeler may submit a full contract with `ontology_submit`, edit a saved draft
with atomic patches, use typed author operations, or materialize and modify a
candidate graph. Work items and component validation are optional. There is no
mandatory tool order, mapping registration, current mapping gate or requirement
to validate components before submission. Ordinary prose and repeated reads are
allowed. Successful completion requires an explicit validated submission, never
an extracted prose object.

`ontology_draft(action="submit", base_hash=...)` and full contract submission use
the same final validation. The reviewer receives that candidate's exact graph,
contract, evidence and hash, with read-only graph capabilities. Revisions return
to the modeler with actionable issues. Request/token counters persist across
sessions; time, format, semantic and transport failures remain distinguishable.
Unknown provider token usage is reported rather than estimated; an explicit
token budget cannot be bypassed by missing usage.
Interrupted reviews preserve successful read-only checks bound to the exact
candidate hash and source fingerprint. `ontology_inspect(resource="review_checks")`
returns those checks with byte pagination. They contain actual tool results and
never an inferred approval; the reviewer still submits a fresh formal verdict.

Drafts, candidate states, tool logs and failure records remain under this run.
A truncated response preserves the current checkpoint. Submission success ends
the role immediately and does not require an additional model acknowledgment.
Reviewer transport/format recovery retains the validated contract and candidate;
only semantic revision sends the task back to the modeler. Published versions save
the effective reviewer configuration, so later analysis maintenance does not
silently fall back to another local model.

The wire adapter adds redundant type declarations for shallow GLM tool parsers;
the original unions, required fields, bounds and Python validation still apply.
It decodes at most one JSON layer for explicitly declared object/array arguments.
Numeric strings and invalid literal metadata remain errors. Original arguments
are retained before Pi's lossy conversion, including valid JSON null values.
GraphRef's schema also exposes its mutually exclusive revision/workspace choice.
Graph parameter errors use `GRAPH_ARGUMENT_INVALID`; review timeouts return
`pending_review` with `review_diagnostics` and a recoverable candidate reference,
instead of being mixed into contract semantic failures.

## Shared Python API and tools

```python
from dataelf.ontology.graph import GraphService, GraphRef
service = GraphService("/tmp/job/ontology/graphs", evidence_root="/tmp/job")
base = GraphRef(graph_id="orders", revision_id="r-...")
candidate = service.workspace("create", base=base)
answer = service.query(base, "SELECT ?s ?o WHERE {?s <urn:retail:amount> ?o}")
```

`GraphRef` identifies exactly one immutable revision or one candidate workspace.
`head.json` is only for discovering the latest revision. Reports bind explicit
versions. Analysis gets its input reference in `DiscoveryContext`; newly used
versions are recorded in `artifacts/analysis_graph_versions.json` and the job's
`workspace_index.json`. Server analysis manifests also include `graph_ref`.

The common Pi extension is loaded by ontology roles and the CLI/Serve analysis
executor. It delegates all operations to the Python service. The tool declarations expose the generated JSON Schemas directly to Pi.

| Tool | Purpose |
|---|---|
| `graph_workspace` | Create, open or discard an explicit candidate |
| `graph_inspect` | Summary or exact byte-paged metadata resources |
| `graph_query` | Local SELECT, ASK or CONSTRUCT |
| `graph_explore` | Incoming/outgoing neighborhoods, actual paths, subgraphs, evidence |
| `graph_materialize` | Execute a contract against source snapshots; replay maintenance |
| `graph_update` | Atomic structured operations or scoped SPARQL Update |
| `graph_diff` | Quad differences and affected entities/graphs |
| `graph_validate` | Check current candidate or revision |
| `graph_commit` | Validate, bind review policy and atomically publish |

SELECT results preserve variable names, typed terms and unbound `null` values.
CONSTRUCT returns typed triples and records query/read scope; input named graph
membership is not retained and query execution is not a per-fact derivation
proof. Neighborhood/path results contain actual edges and nodes. Property-path
reachability is never represented as an actual explanatory path.

Query processes enforce time, CPU and memory limits. Results have bounded pages
and explicit truncation. CONSTRUCT materializes a canonical, sorted result snapshot
once (at most 100,000 triples / 64 MiB, within the query time/memory budget).
Use `next_cursor` for later pages; its blank node labels belong to that snapshot.
`snapshot_is_current=false` explicitly reports changes to a workspace after the
query. Offset-only CONSTRUCT continuation requires the same snapshot; restart
at offset 0 after changes. SELECT offset pages rerun the query, so use ORDER BY.
Parsed syntax admits local query/update forms and rejects SERVICE,
FROM, USING, WITH, LOAD and graph administration. Structured updates and SPARQL
updates share the same atomic working-copy backend. Display truncation never
limits the mutations actually executed. Contract validation queries use the
same bounded backend; SELECT checks above 1000 required rows must be expressed
as an aggregate ASK check.

## Facts, views and provenance

RDF terms distinguish IRI, literal and blank nodes. Literals retain their lexical
value, datatype and language; `1` as an integer is not the string `"1"`.
Published dataset hashes canonicalize a quad-preserving representation, including
blank nodes, rather than hashing RDF text. On RDF import, blank nodes are
canonicalized then skolemized under the logical graph ID; the resulting IRIs
persist across that graph's revisions. Native blank labels are scoped to their
explicit graph state, never global identifiers shared across unrelated imports.

Existing schema/domain/observation/source named graph semantics remain. Default
analysis reads their current snapshot and excludes the separate inference graph;
explicit graph lists control read scope. Candidates and historical revisions are
separate datasets. Validation explicitly records data/schema scope, checks all current data graphs
including inference, and uses no additional inference. It retains existing application domain/range checks and
cardinality/SPARQL checks. Declared property kind, datatype, domain and range are
checked directly against the contract, independently of mutable schema assertions.
V1 `kind=iri` requires an IRI object; blank nodes remain legal for undeclared RDF
predicates. Plain strings and xsd:string are equivalent; language literals require
the corresponding declared datatype. Equivalent cardinality constraints are also published
as SHACL shapes in `constraints.ttl`; validation currently executes the existing
application validator, not a new general SHACL/OWL engine.

Changes require a base version, idempotency key, reason and evidence. They may
include an expected candidate hash. Supported operations are `add_fact`,
`replace_fact` with exact old/new quads, `retract_fact` in its named graph with an
optional source assertion, and `delete_entity` with explicit graph scope. Entity
deletion removes incident edges in that scope without deleting neighbors. A
failed batch leaves the entire candidate unchanged.

Exact fact IDs bind typed quads to multiple supports. Supports record source
file/pointer or premise fact IDs, mapping/query, execution activity and optional
agent/run IDs. Materialization supplies fact-level provenance alongside legacy
node lineage. Removing one source keeps facts supported by other sources.
Removing a final premise marks dependent inferences stale and blocks commit
transitively, accounting for independent valid supports. Premises must be live,
supported and fresh. Returning a premise does not re-certify an invalidated old
derivation; adding a new support activity (or retracting/re-adding with valid
premises) explicitly revalidates the fact. Sources are immutable; new acquisition
should use a new file. Maintenance changes save evidence hashes and occurrence
identity guards. Rebuilding verifies those hashes before replay, retains activity
links, and rejects changed positional identities rather than moving an annotation
onto another array element. Replay conflicts leave the old candidate untouched.
Current graph errors are recalculated; historical execution diagnostics remain
visible separately. Contract/extraction coverage failures still block publication.
Metadata is stored separately from business quads to preserve template regressions.
Imported graphs lacking detailed provenance are explicitly marked `limited`;
missing derivation proofs are never invented.

Example update:

```json
{
  "graph_ref":{"graph_id":"orders","workspace_id":"candidate-id"},
  "changes":{
    "base_revision":"r-parent",
    "idempotency_key":"add-note-1",
    "reason":"Observed in the supplied source",
    "evidence":[{"file":"raw/orders.json","pointer":"/0"}],
    "operations":[{"action":"add_fact","quad":{
      "subject":{"kind":"iri","value":"urn:retail:order/o1"},
      "predicate":{"kind":"iri","value":"urn:retail:note"},
      "object":{"kind":"literal","value":"名称","lang":"zh"},
      "graph":"urn:retail:graph/domain"
    }}]
  }
}
```

## Publication and maintenance

Template execution may publish deterministically through the trusted Python
entry point. Agent maintenance defaults to independent review, including working
copies opened from template revisions. `graph_workspace.review_policy` cannot
lower the host's policy. A trusted caller may configure
`ontology.maintenance_review_policy: deterministic` explicitly; agents cannot
change this through graph tool parameters. Existing candidates are elevated at
agent mutation/commit boundaries. Reviewer has a compact `review_context` resource
with the current delta, affected constraints and evidence; this does not reduce
global validation or impose a tool sequence.

Candidate state is one atomic, durable JSON snapshot. Published revisions are
immutable bundles:

```text
ontology/graphs/<graph_id>/
  revisions/<revision_id>/
    manifest.json graph.nq graph.nt graph.rdf contract.json
    validation.json review.json provenance.json changes.json
    projection_lineage.json state.json constraints.ttl [template.json]
  workspaces/<workspace_id>/state.json
  head.json
```

The new manifest version is `dataelf.ontology.graph.manifest.v2`. Revision IDs
and semantic content hashes are distinct. `.nq` is authoritative; `.nt/.rdf` are
explicit union projections. Before publishing, all files are generated, hashed
and checked, including RDF semantic round-trip. A graph lock and expected parent
prevent concurrent overwrite. Directory promotion and head replacement are
atomic and durable. Commit retries recover their existing revision, including
crashes around head publication; partial staging directories are never heads.
`GraphStore.recover(graph_id)` removes abandoned staging directories.

`graph_commit` automatically validates. Required review returns `pending_review`
from the Python API; `service.review(ref, candidate_hash, review)` attaches a
review bound to exact graph, contract, constraints and provenance. Edits invalidate
it. The analysis tool coordinates an independent Pi reviewer when needed, then
publishes; it adds no human approval step. An explicit `deterministic` maintenance
policy can be selected by a caller. Reviewer approval cannot bypass invalid data.

`OntologyRunResult` now includes `graph_id`, `revision_id`, `graph_ref`,
`bundle_path` and `contract_path`. Deprecated `stage1_bundle/stage2_bundle` both
alias the same revision; `stage2_run_id` is unset because there is no second model
stage. Existing artifact IDs remain available for callers. `validate_result`
reads both manifest versions. `service.import_rdf` accepts N-Quads, N-Triples,
RDF/XML, Turtle and TriG; a supplied manifest must verify and bind the imported
RDF, and contributes its contract/source metadata.

Template mode executes a complete fixed contract without any Pi session.
`save_as_template` exports only when replay reproduces the entire reviewed
candidate. Supplemental patches that cannot be reproduced block template export
with an explicit diagnostic and never publish a misleading template. Rebuilding
a candidate replays saved maintenance with exact fact/evidence preconditions;
version-bound expected_hash requires explicit rebasing and never disappears
silently. Conflicts leave the candidate unchanged. Successful rebuilds archive
the previous candidate under its workspace/rebuilds directory, including explicit resets. Bindings remain portable source selections, separate
from snapshot maintenance changes.

Select `--workflow-policy ai_index_cli_skill_review_v1` explicitly for reviewed AI Index research. Legacy domains and the HTTP service retain their own entrypoints and policies.
