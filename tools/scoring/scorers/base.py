"""Base class for all scoring methods."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

SCORE_INVALID = -1.0
SCORE_RANGE = (0.0, 5.0)


def normalize_scores(
    raw_scores: list[float],
    *,
    higher_is_better: bool = True,
    invalid_threshold: float = -0.5,
    target_min: float = SCORE_RANGE[0],
    target_max: float = SCORE_RANGE[1],
) -> list[float]:
    """Min-max normalize raw scores to [target_min, target_max].

    Args:
        raw_scores: Raw score values; values <= *invalid_threshold* are
            treated as invalid and mapped to -1.
        higher_is_better: If True, higher raw scores map to higher
            normalized scores.  If False, the mapping is inverted (lowest
            raw score -> target_max).
        invalid_threshold: Scores at or below this value are considered
            invalid (e.g. scorer returned -1 for failure).
        target_min: Lower bound of the output range.
        target_max: Upper bound of the output range.

    Returns:
        List of normalized scores (same length as *raw_scores*).
    """
    valid = [s for s in raw_scores if s > invalid_threshold]

    if not valid:
        return [SCORE_INVALID] * len(raw_scores)

    lo, hi = min(valid), max(valid)
    span = hi - lo if hi > lo else 1.0
    target_span = target_max - target_min

    results: list[float] = []
    for s in raw_scores:
        if s <= invalid_threshold:
            results.append(SCORE_INVALID)
        elif higher_is_better:
            results.append(
                round(target_min + ((s - lo) / span) * target_span, 4)
            )
        else:
            results.append(
                round(target_min + ((hi - s) / span) * target_span, 4)
            )
    return results


class BaseScorer(ABC):
    """Unified interface for data quality scorers.

    All scorers must implement the async ``score`` method which accepts a list
    of data records and an output directory, and returns a list of dicts each
    containing at least a ``"score"`` key.
    """

    @abstractmethod
    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        """Score *data* and persist intermediate results under *output_dir*.

        Returns:
            A list of dicts, one per input record, each with at least a
            ``"score"`` field (float).  A score of ``-1`` indicates failure.
            Scores are normalized to [0, 5] with higher = better quality.
        """
