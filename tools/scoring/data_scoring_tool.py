"""Data scoring tool for DataElf.

Supports multiple scoring methods:
  - dataelf:      Hybrid rank-fusion of ifd + deita_q (default, recommended)
  - ask_llm:      Local model log-prob scoring (internal)
  - ppl:          Perplexity scoring (internal PPLScorer)
  - ifd:          Instruction-Following Difficulty (internal IFDScorer)
  - norm_loss:    Normalized loss scoring (internal NormLossScorer)
  - deita_q:      Quality scoring (internal DeitaQualityScorer, 1-6 scale)
  - deita_c:      Complexity scoring (internal DeitaComplexityScorer, 1-6 scale)
  - deberta:      DeBERTa quality classifier (internal DebertaQualityScorer)
  - fineweb_edu:  FineWeb educational quality (internal FinewebEduScorer)

Default parameters are read from DataElf config `tool_defaults.data_scoring`.
Tool kwargs can override any value on a per-call basis.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..base_tool import BaseTool, ToolContext
from ..common.io import load_json, save_json
from .scorers.ask_llm import AskLlmScorer
from .scorers.ppl import PPLScorer
from .scorers.norm_loss import NormLossScorer
from .scorers.ifd import IFDScorer
from .scorers.deita_quality import DeitaQualityScorer
from .scorers.deita_complexity import DeitaComplexityScorer
from .scorers.deberta_quality import DebertaQualityScorer
from .scorers.fineweb_edu import FinewebEduScorer
from .scorers.hybrid import HybridScorer

logger = logging.getLogger(__name__)

SCORER_MAP: dict[str, type] = {
    "dataelf": HybridScorer,
    "ask_llm": AskLlmScorer,
    "ppl": PPLScorer,
    "ifd": IFDScorer,
    "norm_loss": NormLossScorer,
    "deita_q": DeitaQualityScorer,
    "deita_c": DeitaComplexityScorer,
    "deberta": DebertaQualityScorer,
    "fineweb_edu": FinewebEduScorer,
}


def _get_tool_defaults(context_config: dict[str, Any]) -> dict[str, Any]:
    tool_defaults = context_config.get("tool_defaults") or {}
    if not isinstance(tool_defaults, dict):
        return {}
    scoring_defaults = tool_defaults.get("data_scoring") or {}
    return scoring_defaults if isinstance(scoring_defaults, dict) else {}


class DataScoringTool(BaseTool):

    @property
    def name(self) -> str:
        return "data_scoring"

    @property
    def description(self) -> str:
        scorer_names = ", ".join(f"'{k}'" for k in SCORER_MAP)
        return (
            f"Score data quality using various methods: {scorer_names}. "
            "Input data should be in Alpaca format with 'instruction', 'input', 'output' fields. "
            "Default parameters are read from DataElf config tool_defaults.data_scoring."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Data records to score (list of dicts). "
                        "Each dict should have 'instruction', 'input', 'output' fields."
                    ),
                },
                "scorer": {
                    "type": "string",
                    "enum": list(SCORER_MAP.keys()),
                    "description": "Scoring method to use",
                    "default": "dataelf",
                },
                "model": {
                    "type": "string",
                    "description": "Model path to use (overrides tool_defaults)",
                },
                "batch_size": {
                    "type": "integer",
                    "description": "Batch size for scoring (overrides tool defaults)",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Directory to save scoring results (auto-created if omitted)",
                },
            },
            "required": ["data"],
        }

    def usage_example(self) -> str:
        return '''scored = run_tool(
    "data_scoring",
    data=data,
    scorer="dataelf"
)'''

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        data = kwargs["data"]
        tool_defaults = _get_tool_defaults(context.config)
        scorer_type = (
            kwargs.get("scorer")
            or tool_defaults.get("scorer")
            or "dataelf"
        )

        context.log(f"Data scoring: method={scorer_type}, records={len(data)}", "info")

        # Check for cached scores
        cache_path = Path("outputs/scores") / scorer_type / "scored_data.json"
        cached = self._load_cache(context, cache_path, len(data))
        if cached is not None:
            return cached

        if scorer_type not in SCORER_MAP:
            valid = ", ".join(SCORER_MAP)
            raise ValueError(
                f"Unknown scorer type: {scorer_type}. Valid options: {valid}"
            )

        scored = self._run_scorer(context, data, kwargs, scorer_type)

        result = self._merge_scores(data, scored)

        valid_scores = [r["score"] for r in result if r.get("score", -1) >= 0]
        avg_score = sum(valid_scores) / len(valid_scores) if valid_scores else 0.0
        n_invalid = len(result) - len(valid_scores)

        context.log(
            f"Scoring complete: {len(valid_scores)} valid, {n_invalid} invalid, "
            f"avg={avg_score:.2f}",
            "info",
        )

        output = {
            "result": result,
            "metadata": {
                "scorer": scorer_type,
                "records_count": len(result),
                "valid_count": len(valid_scores),
                "invalid_count": n_invalid,
                "average_score": round(avg_score, 4),
            },
            "artifacts": {
                "report_md": (
                    f"# Data Scoring Report\n\n"
                    f"**Method:** {scorer_type}\n"
                    f"**Records Scored:** {len(result)}\n"
                    f"**Valid Scores:** {len(valid_scores)}\n"
                    f"**Average Score:** {avg_score:.4f}\n"
                ),
            },
        }

        self._save_cache(context, cache_path, output)
        return output

    # ── scorer dispatch ─────────────────────────────────────────────

    def _run_scorer(
        self,
        context: ToolContext,
        data: list[dict],
        kwargs: dict,
        scorer_type: str,
    ) -> list[dict]:
        tool_defaults = _get_tool_defaults(context.config)
        scorer_defaults = (
            tool_defaults.get(scorer_type)
            if isinstance(tool_defaults.get(scorer_type), dict)
            else {}
        )

        # Resolve dataelf config (also used as fallback for ifd/deita_q)
        dataelf_defaults = (
            tool_defaults.get("dataelf")
            if isinstance(tool_defaults.get("dataelf"), dict)
            else {}
        )

        batch_size = int(
            kwargs.get("batch_size")
            or scorer_defaults.get("batch_size")
            or tool_defaults.get("batch_size")
            or 8
        )
        max_length = int(scorer_defaults.get("max_length") or 2048)
        output_dir = (
            kwargs.get("output_dir")
            or scorer_defaults.get("output_dir")
            or tool_defaults.get("output_dir")
            or str(Path("outputs/scores") / scorer_type)
        )

        cls = SCORER_MAP[scorer_type]

        if scorer_type == "dataelf":
            ifd_model = dataelf_defaults.get("ifd_model")
            deita_q_model = dataelf_defaults.get("deita_q_model")
            if not ifd_model or not deita_q_model:
                raise ValueError(
                    "dataelf requires both ifd_model and deita_q_model. "
                    "Set tool_defaults.data_scoring.dataelf.ifd_model and deita_q_model."
                )
            ifd_batch_size = int(dataelf_defaults.get("ifd_batch_size") or 8192)
            deita_q_batch_size = int(dataelf_defaults.get("deita_q_batch_size") or 128)
            init_kwargs: dict[str, Any] = {
                "max_length": max_length,
                "alpha": float(dataelf_defaults.get("alpha", 0.5)),
                "ifd_model": ifd_model,
                "deita_q_model": deita_q_model,
                "ifd_batch_size": ifd_batch_size,
                "deita_q_batch_size": deita_q_batch_size,
            }
            context.log(
                f"{cls.__name__}: ifd_model={ifd_model} (bs={ifd_batch_size}), "
                f"deita_q_model={deita_q_model} (bs={deita_q_batch_size}), "
                f"alpha={init_kwargs['alpha']}",
                "info",
            )
        else:
            # For ifd/deita_q, fallback to dataelf config for model and batch_size
            model = kwargs.get("model") or scorer_defaults.get("model")
            if not model and scorer_type == "ifd":
                model = dataelf_defaults.get("ifd_model")
            if not model and scorer_type == "deita_q":
                model = dataelf_defaults.get("deita_q_model")
            if not model:
                raise ValueError(
                    f"Cannot resolve model for '{scorer_type}'. "
                    f"Set tool_defaults.data_scoring.{scorer_type}.model in config."
                )
            # batch_size: kwargs > scorer_defaults > dataelf per-scorer > global > 8
            if not kwargs.get("batch_size") and not scorer_defaults.get("batch_size"):
                if scorer_type == "ifd":
                    batch_size = int(dataelf_defaults.get("ifd_batch_size") or batch_size)
                elif scorer_type == "deita_q":
                    batch_size = int(dataelf_defaults.get("deita_q_batch_size") or batch_size)
            init_kwargs = {
                "model": model,
                "batch_size": batch_size,
                "max_length": max_length,
            }
            # ask_llm has extra parameters
            if scorer_type == "ask_llm":
                init_kwargs["prompt"] = scorer_defaults.get(
                    "prompt", "Is the following data high quality? Please answer yes or no.\n\n"
                )
                init_kwargs["yes_token"] = scorer_defaults.get("yes_token", "yes")
                init_kwargs["dtype"] = scorer_defaults.get("dtype")
            context.log(f"{cls.__name__}: model={model}, batch_size={batch_size}", "info")

        scorer = cls(**init_kwargs)
        return asyncio.run(scorer.score(data, output_dir))

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _merge_scores(original: list[dict], scored: list[dict]) -> list[dict]:
        """Merge scorer output back into original data records."""
        result = []
        for item, score_info in zip(original, scored):
            merged = {**item, "score": score_info.get("score", -1)}
            if "review" in score_info:
                merged["review"] = score_info["review"]
            result.append(merged)

        for item in original[len(scored):]:
            result.append({**item, "score": -1})

        return result

    # ── score caching ─────────────────────────────────────────────────

    @staticmethod
    def _load_cache(
        context: ToolContext, cache_path: Path, expected_count: int,
    ) -> dict[str, Any] | None:
        if not cache_path.exists():
            return None
        try:
            cached = load_json(cache_path)
            if not isinstance(cached, dict) or "result" not in cached:
                return None
            result = cached["result"]
            if len(result) != expected_count:
                context.log(
                    f"Score cache mismatch: cached {len(result)} records "
                    f"vs expected {expected_count}, re-scoring",
                    "warning",
                )
                return None
            scorer = cached.get("metadata", {}).get("scorer", "unknown")
            avg = cached.get("metadata", {}).get("average_score", 0)
            context.log(
                f"Loaded cached scores from {cache_path} "
                f"({len(result)} records, scorer={scorer}, avg={avg})",
                "info",
            )
            return cached
        except Exception as e:
            context.log(f"Failed to load score cache: {e}", "warning")
            return None

    @staticmethod
    def _save_cache(context: ToolContext, cache_path: Path, output: dict[str, Any]) -> None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            save_json(output, cache_path)
            context.log(f"Saved scores to {cache_path} for reuse", "info")
        except Exception as e:
            context.log(f"Failed to save score cache: {e}", "warning")
