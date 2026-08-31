from __future__ import annotations

import json
from typing import Any


COMPILER_SYSTEM = """You compile a reviewed ontology grounding contract into a deterministic raw-JSON extraction plan.
The ontology is an absolute boundary. Never invent classes, properties, JSON paths, facts, defaults, IRIs, code, or triples.
The controller supplies a complete, already Stage 1-bound safe seed. Audit it against the endpoint evidence and return its
entire coverageKey set exactly once. The compiler is not allowed to silently delete controller-required coverage; report any
concern in the rationale for the independent reviewer. Do not retransmit operation objects.
"""


REVIEWER_SYSTEM = """You are an independent knowledge-graph extraction reviewer in a fresh context.
Use the read-only review tools before deciding. Check source replay, information completeness, observation/entity separation,
missingness, association endpoints, identity, relation authority, serialization, and competency-query evidence.
Apply the controller-validated identityPolicy in the review context. Direct endpoint businessIdPath rules do not apply to
cross-endpoint reference operations. A grounded reference-only entity is not an unresolved reference merely because its
independently paginated endpoint omitted it or because its opaque business ID resembles human-readable text.
Critical, high, or medium issues require revise. Never approve based only on the generator rationale.
"""


def compiler_prompt(
    *,
    endpoint: str,
    seed: dict[str, Any],
    endpoint_profile: dict[str, Any],
    feedback: dict[str, Any] | None = None,
) -> str:
    packet = {
        "endpoint": endpoint,
        "endpointProfile": endpoint_profile,
        "safeControllerSeed": seed,
        "repairFeedback": feedback,
    }
    return (
        "Audit the safe controller seed against the endpoint profile. Submit the complete coverageKey set exactly as "
        "provided: every seed operation is Stage 1-bound and controller-required. Do not omit a key; put any concern in "
        "compilerRationale for the independent reviewer. Do not output RDF, operation objects, or prose.\n\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


def reviewer_prompt(context_summary: dict[str, Any]) -> str:
    return (
        "Review the Stage 2 candidate. Call the summary, plans, metrics, samples, and competency-query tools as needed, "
        "then submit exactly one structured verdict.\n\nCandidate identity:\n"
        + json.dumps(context_summary, ensure_ascii=False, sort_keys=True)
    )


__all__ = ["COMPILER_SYSTEM", "REVIEWER_SYSTEM", "compiler_prompt", "reviewer_prompt"]
