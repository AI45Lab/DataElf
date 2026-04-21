# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from tools.base_tool import ToolContext
from tools.scitools.scientific_base_tool import ScientificBaseTool, timed
from tools.scitools.bio.protein_analyzer import run_protein_analyzer


class ProteinAnalyzerTool(ScientificBaseTool):

    domain = "bio"

    @property
    def name(self) -> str:
        return "protein_analyzer"

    @property
    def description(self) -> str:
        return (
            "Protein sequence physicochemical analysis (BioPython, local) with optional NCBI BLAST "
            "homology search. Accepts a sequence list and outputs MW, pI, stability, hydrophobicity, "
            "secondary structure fractions, and an analytical summary."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":           {"type": "string"},
                            "sequence":     {"type": "string"},
                            "protein_name": {"type": "string"},
                        },
                        "required": ["id", "sequence"],
                    },
                    "description": "Sequence list; each entry must have id and sequence (single-letter AA code)",
                },
                "run_blast": {
                    "type": "boolean",
                    "default": False,
                    "description": "Run NCBI BLAST homology search (requires network, ~30-60s per sequence)",
                },
            },
            "required": ["data"],
        }

    def usage_example(self) -> str:
        return 'run_tool("protein_analyzer", data=[{"id": "P00533", "sequence": "MRPSGTAGAA..."}], run_blast=False)'

    @timed
    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from tools.scitools.bio.protein_analyzer import run_protein_analyzer
        data = self.require_data(kwargs, context)
        if data is None:
            return self.err("data parameter is empty", context)

        run_blast = kwargs.get("run_blast", False)
        context.log(f"ProteinAnalyzer starting — {len(data)} sequence(s), "
                    f"BLAST={'on' if run_blast else 'off'}", "info")

        try:
            raw = run_protein_analyzer(
                source=data,
                run_blast_search=run_blast,
                output_dir=self.get_output_dir(context),
            )
        except Exception as e:
            context.log(f"ProteinAnalyzer unhandled exception: {e}", "error")
            raise

        if raw["status"] == "error":
            return self.err(raw.get("code", "UNKNOWN"), context,
                            repair_hint=raw.get("repair_hint"))

        if raw["status"] == "partial_success":
            context.log(
                f"partial_success: {raw['summary']['n_local_success']} analyzed OK, "
                f"{raw['n_errors']} threw exceptions — check result.errors for details.", "warning")

        context.log(f"Done: {raw['n_records']} record(s)", "info")
        return self.ok(
            result={
                "n_records":  raw["n_records"],
                "n_errors":   raw["n_errors"],
                "n_skipped":  raw["n_skipped"],
                "columns":    raw["columns"],
                "summary":    raw["summary"],
                "errors":     raw["errors"],
            },
            metadata={
                "status":            raw["status"],
                "records_processed": raw["n_records"],
                "n_errors":          raw["n_errors"],
                "blast_enabled":     run_blast,
            },
            artifacts={"output_file": raw["file_path"]},
        )