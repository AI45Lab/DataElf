from __future__ import annotations


ONTOLOGY_BUNDLE_VERSION = "dataelf-ontology-bundle.v1"
REQUIRED_STAGE1_ARTIFACTS = (
    "ontology.json",
    "grounding.json",
    "evidence.json",
    "validation.json",
    "review.json",
    "manifest.json",
)
OPTIONAL_STAGE1_ARTIFACTS = ("codex_audit.json",)
ROW_LOCATOR_FIELDS = ("relativeFile", "dataRowNumber", "canonicalRowHash")

