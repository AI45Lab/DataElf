"""DEITA complexity scorer — single-step generate with softmax expectation.

Uses a specialized model (hkust-nlp/deita-complexity-scorer) that generates
a single token and computes the expected complexity score from the softmax
distribution over 6 numeric token IDs (scores 1-6).

Only evaluates the instruction (query) — does not use the output field.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import torch
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

from .base import BaseScorer, normalize_scores

logger = logging.getLogger(__name__)

COMPLEXITY_TEMPLATE = (
    "You are a helpful assistant. Please identify the complexity score "
    "of the following user query. \n"
    "##Query: {instruction}  \n##Complexity: "
)

SCORE_TOKEN_IDS = [29896, 29906, 29941, 29946, 29945, 29953]
SCORE_VALUES = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


class DeitaComplexityScorer(BaseScorer):
    """Score instruction complexity on a 1-6 scale via DEITA single-step generation."""

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
        self._score_ids: torch.Tensor | None = None
        self._score_values: torch.Tensor | None = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("DeitaComplexityScorer: loading %s", self.model_path)
        self._model = AutoModelForCausalLM.from_pretrained(self.model_path)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        self._model.to(self._device)
        self._model.eval()
        self._score_ids = torch.tensor(
            SCORE_TOKEN_IDS, device=self._device, dtype=torch.long,
        )
        self._score_values = torch.tensor(
            SCORE_VALUES, device=self._device, dtype=torch.float,
        )
        logger.info("DeitaComplexityScorer: ready on %s", self._device)

    @torch.no_grad()
    def _score_batch(self, items: list[dict]) -> list[float]:
        prompts: list[str] = []
        for item in items:
            instr = (item.get("instruction", "") or "").strip()
            input_text = (item.get("input", "") or "").strip()
            if input_text:
                instr = f"{instr}\n{input_text}"
            prompts.append(COMPLEXITY_TEMPLATE.format(instruction=instr))

        enc = self._tokenizer(
            prompts, padding=True, truncation=True,
            max_length=self.max_length, return_tensors="pt",
        ).to(self._device)

        outputs = self._model.generate(
            **enc, max_new_tokens=1,
            return_dict_in_generate=True, output_scores=True,
        )
        first_token_scores = outputs.scores[0]

        try:
            selected = first_token_scores.index_select(1, self._score_ids)
        except Exception:
            return [3.0] * len(items)

        probs = torch.softmax(selected, dim=-1)
        batch_scores = (probs * self._score_values).sum(-1)
        return batch_scores.cpu().tolist()

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        self._ensure_model()
        raw: list[float] = []
        for i in tqdm(
            range(0, len(data), self.batch_size), desc="DeitaComplexityScorer",
        ):
            batch = data[i : i + self.batch_size]
            raw.extend(self._score_batch(batch))
        normed = normalize_scores(raw, higher_is_better=True)
        return [{"score": s} for s in normed]
