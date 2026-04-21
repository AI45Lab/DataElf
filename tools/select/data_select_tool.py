"""Data selection tool for DataElf.

Performs diversity-aware selection via embedding extraction/loading, K-means
clustering, quota allocation, and within-cluster TopK selection.

Default parameters are read from DataElf config `tool_defaults.data_select`.
Tool kwargs can override any value on a per-call basis.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from ..base_tool import BaseTool, ToolContext
from ..common.io import save_json
from .selection.cluster import diverse_select
from .selection.embeddings import extract_embeddings

logger = logging.getLogger(__name__)


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _get_tool_defaults(context_config: dict[str, Any]) -> dict[str, Any]:
    tool_defaults = context_config.get("tool_defaults") or {}
    if not isinstance(tool_defaults, dict):
        return {}
    select_defaults = tool_defaults.get("data_select") or {}
    return select_defaults if isinstance(select_defaults, dict) else {}


class DataSelectTool(BaseTool):

    @property
    def name(self) -> str:
        return "data_select"

    @property
    def description(self) -> str:
        return (
            "Diversity-aware data selection using K-means clustering + within-cluster TopK. "
            "Input data must have a 'score' field (from data_scoring tool). "
            "Embeddings are automatically cached under outputs/embeddings/<dataset_name>/ "
            "and reused across runs. "
            "Default parameters are read from DataElf config tool_defaults.data_select."
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
                        "Scored data records (list of dicts). "
                        "Each dict must have a 'score' field. Records with score < 0 are excluded."
                    ),
                },
                "dataset_name": {
                    "type": "string",
                    "description": (
                        "Name of the dataset (e.g. 'alpaca_data'). "
                        "Used to organize cached embeddings under "
                        "outputs/embeddings/<dataset_name>/embeddings_<N>.npy. "
                        "Should match the name used in load_dataset()."
                    ),
                },
                "budget": {
                    "type": "integer",
                    "description": "Target number of samples to select (overrides tool defaults)",
                },
                "n_clusters": {
                    "type": "integer",
                    "description": "Number of K-means clusters (overrides tool defaults)",
                },
                "strategy": {
                    "type": "string",
                    "description": "Quota allocation: 'proportional', 'uniform', or 'mixed'",
                },
                "alpha": {
                    "type": "number",
                    "description": "Mixing coefficient for 'mixed' strategy (0-1)",
                },
                "dedup": {
                    "type": "boolean",
                    "description": "Run post-selection cosine deduplication",
                },
                "dedup_threshold": {
                    "type": "number",
                    "description": "Cosine similarity threshold for dedup",
                },
                "kmeans_backend": {
                    "type": "string",
                    "description": "K-means backend: 'sklearn' or 'torch'",
                },
                "kmeans_device": {
                    "type": "string",
                    "description": "Torch device for K-means (only when backend='torch')",
                },
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Relative directory to save selection results. "
                        "Convention: use a descriptive name like 'outputs/alpaca_20' "
                        "(dataset name + budget)."
                    ),
                },
            },
            "required": ["data"],
        }

    def usage_example(self) -> str:
        return '''selected = run_tool(
    "data_select",
    data=scored_data,
    dataset_name="alpaca_data",
    budget=20,
    strategy="proportional",
    output_dir="outputs/alpaca_20"
)'''

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        data = kwargs["data"]
        tool_defaults = _get_tool_defaults(context.config)

        budget = kwargs.get("budget") or tool_defaults.get("budget")
        if not budget:
            raise ValueError("'budget' is required (pass directly or set tool_defaults.data_select.budget).")

        context.log(f"Data selection: input={len(data)} records, budget={budget}", "info")

        indexed_data = [{"_idx": i, **item} for i, item in enumerate(data)]
        valid_data = [item for item in indexed_data if item.get("score", -1) >= 0]

        if not valid_data:
            context.log("No valid scored records (all scores < 0)", "error")
            return {
                "result": [],
                "metadata": {"error": "no valid scores"},
                "artifacts": {},
            }

        context.log(
            f"Filtered: {len(valid_data)} valid / {len(data) - len(valid_data)} invalid",
            "info",
        )

        dataset_name = kwargs.get("dataset_name") or "default"
        embeddings = self._load_or_extract_embeddings(context, valid_data, dataset_name, len(data))

        n_clusters = kwargs.get("n_clusters") or tool_defaults.get("n_clusters") or 500
        strategy = kwargs.get("strategy") or tool_defaults.get("strategy") or "proportional"
        alpha = kwargs.get("alpha") if "alpha" in kwargs else tool_defaults.get("alpha", 0.5)
        dedup = kwargs.get("dedup") if "dedup" in kwargs else tool_defaults.get("dedup", False)
        dedup_threshold = kwargs.get("dedup_threshold") or tool_defaults.get("dedup_threshold") or 0.95

        kmeans_defaults = tool_defaults.get("kmeans") if isinstance(tool_defaults.get("kmeans"), dict) else {}
        kmeans_backend = kwargs.get("kmeans_backend") or kmeans_defaults.get("backend") or "sklearn"
        kmeans_device = kwargs.get("kmeans_device") or kmeans_defaults.get("device") or "cuda"

        context.log(
            f"Selection params: K={n_clusters}, budget={budget}, "
            f"strategy={strategy}, backend={kmeans_backend}",
            "info",
        )

        selected = diverse_select(
            embeddings=embeddings,
            data=valid_data,
            n_clusters=n_clusters,
            budget=budget,
            strategy=strategy,
            alpha=alpha,
            dedup=dedup,
            dedup_threshold=dedup_threshold,
            kmeans_backend=kmeans_backend,
            kmeans_device=kmeans_device,
            kmeans_batch_size=kmeans_defaults.get("batch_size") or 10000,
            kmeans_max_iter=kmeans_defaults.get("max_iter") or 100,
            kmeans_n_init=kmeans_defaults.get("n_init") or 3,
            kmeans_verbose=kmeans_defaults.get("verbose") or 1,
        )

        output_dir = kwargs.get("output_dir") or tool_defaults.get("output_dir")
        if output_dir:
            out_path = Path(output_dir) / "diverse_selected.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            save_json(selected, out_path)
            context.log(f"Saved selection to {out_path}", "info")

        context.log(f"Selected {len(selected)} / {len(valid_data)} records", "info")

        return {
            "result": selected,
            "metadata": {
                "input_count": len(data),
                "valid_count": len(valid_data),
                "selected_count": len(selected),
                "n_clusters": n_clusters,
                "budget": budget,
                "strategy": strategy,
            },
            "artifacts": {
                "report_md": (
                    f"# Data Selection Report\n\n"
                    f"**Input Records:** {len(data)}\n"
                    f"**Valid (score >= 0):** {len(valid_data)}\n"
                    f"**Selected:** {len(selected)}\n"
                    f"**Clusters:** {n_clusters}\n"
                    f"**Strategy:** {strategy}\n"
                ),
            },
        }

    def _resolve_embeddings_path(
        self, context: ToolContext, dataset_name: str, total_count: int,
    ) -> Path:
        tool_defaults = _get_tool_defaults(context.config)
        base_dir = tool_defaults.get("embeddings_dir") or "outputs/embeddings"
        return Path(base_dir) / dataset_name / f"embeddings_{total_count}.npy"

    def _load_or_extract_embeddings(
        self,
        context: ToolContext,
        valid_data: list[dict],
        dataset_name: str,
        total_count: int,
    ) -> np.ndarray:
        tool_defaults = _get_tool_defaults(context.config)
        embeddings_path = self._resolve_embeddings_path(context, dataset_name, total_count)

        if embeddings_path.exists():
            loaded = self._load_embeddings(context, str(embeddings_path), valid_data)
            if loaded is not None:
                return loaded

        embedding_model = tool_defaults.get("embedding_model")
        if not embedding_model:
            raise ValueError(
                "No embeddings available and no embedding_model configured. "
                "Set tool_defaults.data_select.embedding_model."
            )

        embeddings = self._extract_embeddings(context, valid_data, embedding_model)
        embeddings_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(embeddings_path), embeddings)
        context.log(f"Saved embeddings to {embeddings_path} for reuse", "info")
        return embeddings

    def _load_embeddings(
        self, context: ToolContext, path: str, valid_data: list[dict],
    ) -> np.ndarray:
        context.log(f"Loading embeddings from {path}", "info")
        embeddings_full = np.load(path)
        context.log(f"Loaded embeddings: shape={embeddings_full.shape}", "info")

        max_idx = max(item["_idx"] for item in valid_data)
        if max_idx >= embeddings_full.shape[0]:
            context.log(
                f"Embeddings file outdated: max data index {max_idx} >= "
                f"embeddings rows {embeddings_full.shape[0]}, will re-extract",
                "warning",
            )
            return None

        valid_indices = [item["_idx"] for item in valid_data]
        embeddings = embeddings_full[valid_indices]
        context.log(f"Aligned {len(embeddings)} embeddings with valid samples", "info")
        return embeddings

    def _extract_embeddings(
        self,
        context: ToolContext,
        valid_data: list[dict],
        model_path: str,
    ) -> np.ndarray:
        tool_defaults = _get_tool_defaults(context.config)
        batch_size = tool_defaults.get("embedding_batch_size") or 64
        max_length = tool_defaults.get("embedding_max_length") or 1024
        dtype = tool_defaults.get("embedding_dtype") or "bfloat16"
        device = tool_defaults.get("embedding_device") or "cuda"

        context.log(
            f"Extracting embeddings on-the-fly: model={model_path}, "
            f"records={len(valid_data)}, batch_size={batch_size}",
            "info",
        )

        clean_data = [{k: v for k, v in item.items() if k != "_idx"} for item in valid_data]

        embeddings = extract_embeddings(
            data=clean_data,
            model_name_or_path=model_path,
            batch_size=batch_size,
            max_length=max_length,
            device=device,
            dtype=dtype,
        )
        context.log(f"Extracted embeddings: shape={embeddings.shape}", "info")
        return embeddings
