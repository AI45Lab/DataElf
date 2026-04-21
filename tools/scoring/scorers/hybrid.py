"""Hybrid scorer (dataelf) for DataElf — rank-fusion of ifd + deita_q.

Runs both sub-scorers, converts raw scores to percentile ranks,
and fuses them with a configurable alpha weight:

    fused = alpha * rank(ifd) + (1 - alpha) * rank(deita_q)

Score cache integration:
  - Before running a sub-scorer, checks outputs/scores/<name>/scored_data.json.
    If cached scores exist with matching record count, reuses them.
  - After running a sub-scorer, saves results to that cache path so future
    standalone runs of ifd/deita_q (or another dataelf run) hit the cache.
  - Output scores are scaled to [0, 5] to match other scorers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .base import BaseScorer, SCORE_RANGE

logger = logging.getLogger(__name__)

SCORE_CACHE_DIR = Path("outputs/scores")

# Sub-scorer names matching the keys in SCORER_MAP / defaults.yaml
_SUB_SCORERS = {
    "ifd": {"class_path": ".ifd.IFDScorer"},
    "deita_q": {"class_path": ".deita_quality.DeitaQualityScorer"},
}


def _percentile_ranks(scores: list[float]) -> list[float]:
    """Convert raw scores to percentile ranks in [0, 1] (1 = best).

    Invalid scores (< 0) get rank 0.
    """
    n = len(scores)
    indexed = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    for rank_pos, orig_idx in enumerate(indexed):
        ranks[orig_idx] = rank_pos / max(n - 1, 1)
    return ranks


def _load_sub_cache(name: str, expected_count: int) -> list[dict] | None:
    """Try to load cached scores for a sub-scorer."""
    cache_path = SCORE_CACHE_DIR / name / "scored_data.json"
    if not cache_path.exists():
        return None
    try:
        blob = json.loads(cache_path.read_text())
        if not isinstance(blob, dict) or "result" not in blob:
            return None
        result = blob["result"]
        if len(result) != expected_count:
            logger.warning(
                f"Sub-scorer cache {name}: count mismatch "
                f"(cached={len(result)}, expected={expected_count}), will re-score"
            )
            return None
        logger.info(f"Loaded cached {name} scores ({len(result)} records)")
        return result
    except Exception as e:
        logger.warning(f"Failed to load {name} cache: {e}")
        return None


def _save_sub_cache(name: str, data: list[dict], scored: list[dict]) -> None:
    """Save sub-scorer results so standalone runs can reuse them."""
    merged = []
    for item, score_info in zip(data, scored):
        merged.append({
            "instruction": item.get("instruction", ""),
            "input": item.get("input", ""),
            "output": item.get("output", ""),
            "score": score_info.get("score", -1),
        })

    valid_scores = [r["score"] for r in merged if r.get("score", -1) >= 0]
    avg = sum(valid_scores) / len(valid_scores) if valid_scores else 0.0

    blob = {
        "result": merged,
        "metadata": {
            "scorer": name,
            "records_count": len(merged),
            "valid_count": len(valid_scores),
            "invalid_count": len(merged) - len(valid_scores),
            "average_score": round(avg, 4),
        },
    }

    cache_path = SCORE_CACHE_DIR / name / "scored_data.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(blob, ensure_ascii=False))
    logger.info(f"Saved {name} scores to {cache_path} for reuse")


class HybridScorer(BaseScorer):
    """Rank-fusion of ifd and deita_q sub-scorers."""

    def __init__(
        self,
        ifd_model: str,
        deita_q_model: str,
        ifd_batch_size: int = 8192,
        deita_q_batch_size: int = 128,
        max_length: int = 2048,
        alpha: float = 0.5,
    ) -> None:
        self.max_length = max_length
        self.alpha = alpha
        self.ifd_model = ifd_model
        self.deita_q_model = deita_q_model
        self.ifd_batch_size = ifd_batch_size
        self.deita_q_batch_size = deita_q_batch_size

    async def _get_sub_scores(
        self, name: str, data: list[dict], output_dir: Path,
    ) -> list[dict]:
        """Load sub-scorer results from cache, or run the scorer and cache results."""
        cached = _load_sub_cache(name, len(data))
        if cached is not None:
            return cached

        # Lazy import sub-scorers to avoid circular imports
        if name == "ifd":
            from .ifd import IFDScorer
            scorer = IFDScorer(
                model=self.ifd_model,
                batch_size=self.ifd_batch_size,
                max_length=self.max_length,
            )
        elif name == "deita_q":
            from .deita_quality import DeitaQualityScorer
            scorer = DeitaQualityScorer(
                model=self.deita_q_model,
                batch_size=self.deita_q_batch_size,
                max_length=self.max_length,
            )
        else:
            raise ValueError(f"Unknown sub-scorer: {name}")

        logger.info(f"HybridScorer: running {name} sub-scorer")
        results = await scorer.score(data, output_dir / name)

        _save_sub_cache(name, data, results)
        return results

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        ifd_results = await self._get_sub_scores("ifd", data, output_dir)
        deita_q_results = await self._get_sub_scores("deita_q", data, output_dir)

        # Extract scores
        ifd_scores = [float(r.get("score", -1.0)) for r in ifd_results]
        deita_q_scores = [float(r.get("score", -1.0)) for r in deita_q_results]

        # Invalid in either scorer → invalid in fusion
        invalid = {
            i for i in range(len(data))
            if ifd_scores[i] < 0 or deita_q_scores[i] < 0
        }

        # Rank fusion → scale to [0, 5]
        ifd_ranks = _percentile_ranks(ifd_scores)
        deita_q_ranks = _percentile_ranks(deita_q_scores)

        fused = []
        for i in range(len(data)):
            if i in invalid:
                fused.append({"score": -1.0})
            else:
                raw = self.alpha * ifd_ranks[i] + (1 - self.alpha) * deita_q_ranks[i]
                scaled = round(raw * SCORE_RANGE[1], 4)  # [0,1] → [0,5]
                fused.append({"score": scaled})

        n_valid = sum(1 for r in fused if r["score"] >= 0)
        logger.info(
            f"HybridScorer: fused {n_valid} valid scores "
            f"(alpha={self.alpha}, scale=[0, {SCORE_RANGE[1]}])"
        )

        return fused
