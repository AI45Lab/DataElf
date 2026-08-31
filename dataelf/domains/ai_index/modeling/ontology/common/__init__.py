"""Contracts shared by ontology construction stages."""

from .artifacts import atomic_write_json, canonical_json_bytes, file_sha256, sha256_json

__all__ = ["atomic_write_json", "canonical_json_bytes", "file_sha256", "sha256_json"]
"""Contracts and artifact helpers shared by ontology stages."""

from dataelf.domains.ai_index.modeling.ontology.common.contracts import (
    ONTOLOGY_BUNDLE_VERSION,
    OPTIONAL_STAGE1_ARTIFACTS,
    REQUIRED_STAGE1_ARTIFACTS,
    ROW_LOCATOR_FIELDS,
)

__all__ = [
    "ONTOLOGY_BUNDLE_VERSION",
    "OPTIONAL_STAGE1_ARTIFACTS",
    "REQUIRED_STAGE1_ARTIFACTS",
    "ROW_LOCATOR_FIELDS",
]
