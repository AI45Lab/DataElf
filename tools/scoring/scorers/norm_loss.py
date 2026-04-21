"""Normalized loss scorer — average cross-entropy in log2 domain.

Computes the per-token cross-entropy loss averaged over valid tokens,
then converts from natural log to log2 (bits).  Lower values indicate
text that is more predictable under the model.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import torch
import torch.nn.functional as F
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

from .base import BaseScorer, normalize_scores

logger = logging.getLogger(__name__)


def _join_text(item: dict) -> str:
    instruction = item.get("instruction", "")
    input_text = item.get("input", "") or item.get("input_text", "")
    output = item.get("output", "")
    if input_text:
        return f"{instruction}\n{input_text}\n{output}"
    return f"{instruction}\n{output}"


class NormLossScorer(BaseScorer):
    """Score data by normalized cross-entropy loss (log2 domain)."""

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

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        logger.info("NormLossScorer: loading %s", self.model_path)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path, device_map="auto", torch_dtype="auto",
        )
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
            self._model.config.pad_token_id = self._tokenizer.eos_token_id
        self._model.eval()
        logger.info("NormLossScorer: ready")

    @torch.inference_mode()
    def _score_batch(self, items: list[dict]) -> list[float]:
        texts = [_join_text(item) for item in items]
        enc = self._tokenizer(
            texts, return_tensors="pt", padding="longest",
            truncation=True, max_length=self.max_length,
        ).to(self._model.device)

        input_ids = enc.input_ids
        attention_mask = enc.attention_mask
        labels = input_ids.clone()
        labels[attention_mask == 0] = -100

        logits = self._model(
            input_ids=input_ids, attention_mask=attention_mask, use_cache=False,
        ).logits

        loss_flat = F.cross_entropy(
            logits[:, :-1, :].contiguous().view(-1, logits.size(-1)),
            labels[:, 1:].contiguous().view(-1),
            reduction="none", ignore_index=-100,
        ).view(input_ids.size(0), -1)

        valid_mask = attention_mask[:, 1:].float()
        scores: list[float] = []
        for i in range(input_ids.size(0)):
            total_loss = (loss_flat[i] * valid_mask[i]).sum().item()
            total_tokens = valid_mask[i].sum().item()
            if total_tokens > 0:
                scores.append((total_loss / total_tokens) / math.log(2))
            else:
                scores.append(0.0)
        return scores

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        self._ensure_model()
        raw: list[float] = []
        for i in tqdm(range(0, len(data), self.batch_size), desc="NormLossScorer"):
            batch = data[i : i + self.batch_size]
            raw.extend(self._score_batch(batch))
        normed = normalize_scores(raw, higher_is_better=False)
        return [{"score": s} for s in normed]
