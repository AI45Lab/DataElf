# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from tools.base_tool import ToolContext
from tools.scitools.scientific_base_tool import ScientificBaseTool, timed
from tools.scitools.bio.enzyme_acquire import run_enzyme_acquire


class EnzymeAcquireTool(ScientificBaseTool):

    domain = "bio"

    @property
    def name(self) -> str:
        return "enzyme_acquire"

    @property
    def description(self) -> str:
        return (
            "Cross-database enzyme attribute retrieval (UniProt / KEGG / PubChem). "
            "Input enzyme name, EC number, or UniProt ID. "
            "Outputs a standardized Parquet table with sequence, reactions, and substrate SMILES, "
            "plus an analytical summary of the retrieved records."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Query list; each entry is an enzyme name, "
                        "EC number (e.g. 1.1.1.1), or UniProt ID (e.g. P00533)."
                    ),
                },
                "max_results":   {"type": "integer", "default": 5},
                "fetch_smiles":  {"type": "boolean",  "default": True},
                "fetch_kegg":    {"type": "boolean",  "default": True},
                "output_format": {
                    "type": "string",
                    "enum": ["parquet", "csv"],
                    "default": "parquet",
                },
            },
            "required": ["data"],
        }

    # add usage_example()
    def usage_example(self) -> str:
        return 'run_tool("enzyme_acquire", data=["1.1.1.1", "P00533", "lipase"])'

    @timed
    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from tools.scitools.bio.enzyme_acquire import run_enzyme_acquire
        queries = self.require_data(kwargs, context)
        if queries is None:
            return self.err("data parameter is empty or missing", context)

        context.log(f"EnzymeAcquire starting — {len(queries)} query(ies)", "info")

        try:
            raw = run_enzyme_acquire(
                queries=queries,
                max_results=kwargs.get("max_results", 5),
                fetch_smiles=kwargs.get("fetch_smiles", True),
                fetch_kegg=kwargs.get("fetch_kegg", True),
                output_format=kwargs.get("output_format", "parquet"),
                output_dir=self.get_output_dir(context),
            )
        except Exception as e:
            context.log(f"EnzymeAcquire unhandled exception: {e}", "error")
            raise

        # Hard failures — no usable output file was produced
        if raw["status"] == "error":
            return self.err(
                raw.get("code", "UNKNOWN"),
                context,
                repair_hint=raw.get("repair_hint"),
            )

        # Log a warning for partial_success so the pipeline is aware
        if raw["status"] == "partial_success":
            context.log(
                f"partial_success: {raw['n_queries_succeeded']} query(ies) OK, "
                f"{raw['n_queries_failed']} failed — check result.errors for details.",
                "warning",
            )

        context.log(
            f"Done: {raw['n_records']} record(s) | "
            f"{raw['n_queries_succeeded']}/{raw['n_queries_requested']} queries succeeded",
            "info",
        )

        return self.ok(
            result={
                # Query-level statistics
                "n_queries_requested":  raw["n_queries_requested"],
                "n_queries_succeeded":  raw["n_queries_succeeded"],
                "n_queries_failed":     raw["n_queries_failed"],
                "n_skipped":            raw["n_skipped"],
                # Record-level statistics
                "n_records":            raw["n_records"],
                "n_duplicates_dropped": raw["n_duplicates_dropped"],
                # Output schema
                "columns":              raw["columns"],
                # Analytical summary
                "summary":              raw["summary"],
                # Error detail (empty list on full success)
                "errors":               raw["errors"],
            },
            metadata={
                "status":            raw["status"],   # success | partial_success
                "records_processed": raw["n_records"],
                "n_errors":          raw["n_queries_failed"],
            },
            artifacts={
                "output_file": raw["file_path"],
            },
        )