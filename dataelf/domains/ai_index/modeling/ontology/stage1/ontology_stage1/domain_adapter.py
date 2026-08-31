from __future__ import annotations

from typing import Any, Protocol

from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config


class Stage1DomainAdapter(Protocol):
    generator_system: str
    reviewer_system: str
    source_endpoint_targets: dict[str, tuple[str, str]]

    def candidate_from_semantic_plan(
        self,
        plan: dict[str, Any],
        config: Stage1Config,
        source_fingerprint: str,
    ) -> dict[str, Any]: ...

    def normalize_candidate_contract(
        self,
        candidate: dict[str, Any],
        evidence: dict[str, Any],
        config: Stage1Config,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def generator_prompt(self, *args: Any, **kwargs: Any) -> str: ...

    def semantic_plan_prompt(self, *args: Any, **kwargs: Any) -> str: ...

    def reviewer_prompt(self, *args: Any, **kwargs: Any) -> str: ...

    def compact_reviewer_prompt(self, *args: Any, **kwargs: Any) -> str: ...

    def prompt_fingerprint(self) -> str: ...

    def repair_feedback(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def validate_candidate(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def validate_review(self, *args: Any, **kwargs: Any) -> list[str]: ...

    def build_shacl_ttl(self, ontology: dict[str, Any]) -> str: ...

    def shacl_contract_errors(self, ontology: dict[str, Any], shacl_ttl: str) -> list[str]: ...


__all__ = ["Stage1DomainAdapter"]
