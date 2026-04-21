"""DeBERTa quality classifier scorer.

Uses nvidia/quality-classifier-deberta — a DeBERTa-v3-base encoder with a
linear classification head that predicts quality labels (High=0, Medium=1, Low=2).
Returns the argmax class index as the score.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn
from tqdm import tqdm
from transformers import AutoModel, AutoTokenizer

from .base import BaseScorer, normalize_scores

logger = logging.getLogger(__name__)

try:
    from huggingface_hub import PyTorchModelHubMixin
except ImportError:
    PyTorchModelHubMixin = object


class QualityModel(nn.Module, PyTorchModelHubMixin):
    """Custom classification head on top of a DeBERTa base model."""

    def __init__(self, config: dict) -> None:
        super().__init__()
        self.model = AutoModel.from_pretrained(config["base_model"])
        self.dropout = nn.Dropout(config["fc_dropout"])
        self.fc = nn.Linear(
            self.model.config.hidden_size, len(config["id2label"]),
        )

    def forward(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor,
    ) -> torch.Tensor:
        features = self.model(
            input_ids=input_ids, attention_mask=attention_mask,
        ).last_hidden_state
        dropped = self.dropout(features)
        outputs = self.fc(dropped)
        return torch.softmax(outputs[:, 0, :], dim=1)


class DebertaQualityScorer(BaseScorer):
    """Score data quality using nvidia/quality-classifier-deberta (argmax class)."""

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
        logger.info("DebertaQualityScorer: loading %s", self.model_path)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        self._model = QualityModel.from_pretrained(self.model_path)
        self._model = self._model.float().to(self._device)
        self._model.eval()
        logger.info("DebertaQualityScorer: ready on %s", self._device)

    @torch.no_grad()
    def _score_batch(self, items: list[dict]) -> list[float]:
        texts: list[str] = []
        for item in items:
            text = item.get("instruction", "")
            input_text = item.get("input", "")
            if input_text:
                text += "\n" + input_text
            text += "\n" + item.get("output", "")
            texts.append(text)

        enc = self._tokenizer(
            texts, return_tensors="pt", padding=True,
            truncation=True, max_length=self.max_length,
        ).to(self._device)

        outputs = self._model(enc["input_ids"], enc["attention_mask"])
        predicted = torch.argmax(outputs, dim=1)
        return predicted.cpu().tolist()

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        self._ensure_model()
        raw: list[float] = []
        for i in tqdm(
            range(0, len(data), self.batch_size), desc="DebertaQualityScorer",
        ):
            batch = data[i : i + self.batch_size]
            raw.extend(float(s) for s in self._score_batch(batch))
        # 0=High, 1=Medium, 2=Low → lower is better quality
        normed = normalize_scores(raw, higher_is_better=False, invalid_threshold=-0.5)
        return [{"score": s} for s in normed]
