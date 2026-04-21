"""FineWeb educational quality scorer.

Uses HuggingFaceFW/fineweb-edu-classifier — a sequence classification model
that outputs a scalar logit representing educational quality.
Higher values indicate more educationally valuable content.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import torch
from tqdm import tqdm
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from .base import BaseScorer, normalize_scores

logger = logging.getLogger(__name__)


class FinewebEduScorer(BaseScorer):
    """Score educational quality using FineWeb-Edu classifier (scalar logit)."""

    def __init__(
        self,
        model: str,
        batch_size: int = 32,
        max_length: int = 2048,
    ) -> None:
        self.model_path = model
        self.batch_size = batch_size
        self.max_length = max_length
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = ""

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("FinewebEduScorer: loading %s", self.model_path)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self.model_path,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        self._model.to(self._device)
        self._model.eval()
        logger.info("FinewebEduScorer: ready on %s", self._device)

    @torch.no_grad()
    def _score_batch(self, items: list[dict]) -> list[float]:
        texts: list[str] = []
        for item in items:
            parts = [
                item.get("instruction", ""),
                item.get("input", ""),
                item.get("output", ""),
            ]
            texts.append("\n".join(parts))

        enc = self._tokenizer(
            texts, return_tensors="pt", padding="longest",
            truncation=True, max_length=self.max_length,
        ).to(self._device)

        outputs = self._model(**enc)
        logits = outputs.logits.squeeze(-1).float()
        scores = logits.cpu().tolist()
        if not isinstance(scores, list):
            scores = [scores]
        return scores

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        self._ensure_model()
        raw: list[float] = []
        for i in tqdm(range(0, len(data), self.batch_size), desc="FinewebEduScorer"):
            batch = data[i : i + self.batch_size]
            raw.extend(self._score_batch(batch))
        normed = normalize_scores(raw, higher_is_better=True, invalid_threshold=-100.0)
        return [{"score": s} for s in normed]
