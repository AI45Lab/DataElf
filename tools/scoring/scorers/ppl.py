"""PPL (Perplexity) scorer — full-sequence perplexity via causal LM.

Computes exp(mean NLL) over the entire token sequence (instruction + input + output).
Lower perplexity indicates more "typical" text under the model.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import torch
import torch.nn.functional as F
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

from .base import BaseScorer, normalize_scores

logger = logging.getLogger(__name__)


def _join_text(item: dict) -> str:
    parts = [
        item.get("instruction", ""),
        item.get("input", ""),
        item.get("output", ""),
    ]
    return "\n".join(p for p in parts if p)


class PPLScorer(BaseScorer):
    """Score data by computing full-sequence perplexity with a causal LM."""

    def __init__(
        self,
        model: str,
        batch_size: int = 8,
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
        logger.info("PPLScorer: loading %s", self.model_path)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path, device_map="auto", torch_dtype="auto",
        )
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
            self._model.config.pad_token_id = self._tokenizer.eos_token_id
        self._model.eval()
        logger.info("PPLScorer: ready on %s", self._device)

    @torch.inference_mode()
    def _score_batch(self, items: list[dict]) -> list[float]:
        texts = [_join_text(item) for item in items]
        enc = self._tokenizer(
            texts, return_tensors="pt", padding=True,
            truncation=True, max_length=self.max_length,
        ).to(self._model.device)

        input_ids = enc.input_ids
        attention_mask = enc.attention_mask
        labels = input_ids.clone()
        labels[attention_mask == 0] = -100

        logits = self._model(
            input_ids=input_ids, attention_mask=attention_mask, use_cache=False,
        ).logits

        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()

        loss_flat = F.cross_entropy(
            shift_logits.view(-1, shift_logits.size(-1)),
            shift_labels.view(-1),
            reduction="none", ignore_index=-100,
        ).view(shift_labels.size())

        valid = shift_labels != -100
        denom = valid.sum(dim=1).clamp_min(1)
        avg_loss = (loss_flat * valid.float()).sum(dim=1) / denom.float()
        ppl = torch.exp(avg_loss)

        return ppl.cpu().tolist()

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        self._ensure_model()
        raw: list[float] = []
        for i in tqdm(range(0, len(data), self.batch_size), desc="PPLScorer"):
            batch = data[i : i + self.batch_size]
            raw.extend(self._score_batch(batch))
        normed = normalize_scores(raw, higher_is_better=False)
        return [{"score": s} for s in normed]
