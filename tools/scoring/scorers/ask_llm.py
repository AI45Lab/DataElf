"""AskLlm scorer — local model log-prob scoring.

Computes the average log P(yes_token | prompt, data) using a causal LM.
For single-token yes_token returns its log probability; for multi-token
yes_token returns the mean across all token log probabilities.

This is a self-contained implementation that does NOT depend on any
external repository.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

from ...common.io import (
    append_jsonl,
    load_jsonl_id_set,
    normalize_id,
    repair_trailing_incomplete_jsonl,
    save_json,
)
from .base import BaseScorer

logger = logging.getLogger(__name__)

TORCH_DTYPE_MAP = {
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
    "float32": torch.float32,
}


def _as_text(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _sanitize_record(item: dict, record_id: int) -> dict:
    record = dict(item)
    record["instruction"] = _as_text(record.get("instruction", ""))
    record["input"] = _as_text(record.get("input", ""))
    record["output"] = _as_text(record.get("output", ""))
    record["id"] = record_id
    return record


class AskLlmScorer(BaseScorer):
    """Score data quality by computing log P(yes_token | prompt + data)."""

    def __init__(
        self,
        model: str,
        prompt: str = "Is the following data high quality? Please answer yes or no.\n\n",
        yes_token: str = "yes",
        batch_size: int = 8,
        max_length: int = 2048,
        dtype: str | None = None,
    ) -> None:
        self.model_path = model
        self.prompt = prompt
        self.yes_token = yes_token
        self.batch_size = batch_size
        self.max_length = max_length
        self.dtype_override = dtype

        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = ""

    # ── lazy model loading ────────────────────────────────────────────

    def _ensure_model(self) -> None:
        if self._model is not None:
            return

        self._device = "cuda" if torch.cuda.is_available() else "cpu"

        if self.dtype_override and self.dtype_override in TORCH_DTYPE_MAP:
            torch_dtype = TORCH_DTYPE_MAP[self.dtype_override]
        elif self._device == "cuda":
            torch_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        else:
            torch_dtype = torch.float32

        logger.info(
            "Loading model %s (dtype=%s, device=%s)",
            self.model_path, torch_dtype, self._device,
        )

        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            torch_dtype=torch_dtype,
            low_cpu_mem_usage=True,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)

        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
            self._tokenizer.pad_token_id = self._tokenizer.eos_token_id

        if self._device == "cuda":
            self._model.to(self._device)
        self._model.eval()

        logger.info("Model loaded successfully")

    # ── core scoring ──────────────────────────────────────────────────

    def _format_item(self, item: dict) -> str:
        instruction = _as_text(item.get("instruction", ""))
        input_text = _as_text(item.get("input", ""))
        output = _as_text(item.get("output", ""))

        data_text = (
            f"{instruction}\n{input_text}\n{output}\n"
            if input_text
            else f"{instruction}\n{output}\n"
        )
        return self.prompt + data_text + "\n\n"

    @torch.no_grad()
    def score_batch(self, data_items: list[dict]) -> list[float]:
        """Compute average log P(yes_token) for a batch of items."""
        if not data_items:
            return []

        self._ensure_model()

        prompts = [self._format_item(item) for item in data_items]
        full_texts = [p + self.yes_token for p in prompts]

        prompt_tokens = self._tokenizer(
            prompts,
            return_tensors="pt",
            add_special_tokens=False,
            padding=True,
        )
        prompt_lengths = prompt_tokens.attention_mask.sum(dim=1).tolist()

        full_tokens = self._tokenizer(
            full_texts,
            return_tensors="pt",
            add_special_tokens=False,
            padding=True,
            truncation=True,
            max_length=self.max_length,
        )

        input_ids = full_tokens.input_ids.to(self._device)
        attention_mask = full_tokens.attention_mask.to(self._device)

        outputs = self._model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            return_dict=True,
        )
        logits = outputs.logits

        batch_scores: list[float] = []
        for idx in range(input_ids.shape[0]):
            prompt_len = prompt_lengths[idx]
            sample_len = int(attention_mask[idx].sum().item())

            if prompt_len >= self.max_length or prompt_len >= sample_len:
                batch_scores.append(-100.0)
                continue

            log_probs: list[float] = []
            for i in range(prompt_len, sample_len):
                if attention_mask[idx, i].item() == 0:
                    break
                true_token_id = input_ids[idx, i].item()
                if true_token_id == self._tokenizer.pad_token_id:
                    continue
                # logits at position i-1 predict token at position i
                current_logits = logits[idx, i - 1, :].float()
                log_probs_all = F.log_softmax(current_logits, dim=-1)
                log_probs.append(log_probs_all[true_token_id].item())

            batch_scores.append(float(np.mean(log_probs)) if log_probs else -100.0)

        return batch_scores

    # ── file-based evaluation with checkpoint/resume ──────────────────

    def _evaluate_to_file(
        self, input_jsonl: Path, output_jsonl: Path,
    ) -> None:
        total = sum(1 for _ in open(input_jsonl, "r", encoding="utf-8"))

        done_ids: set[str] = set()
        if output_jsonl.exists():
            repair_trailing_incomplete_jsonl(output_jsonl)
            done_ids = load_jsonl_id_set(output_jsonl, id_key="id")
            if done_ids:
                logger.info("Resuming: %d items already scored", len(done_ids))

        buf_items: list[dict] = []
        buf_ids: list[Any] = []

        with open(input_jsonl, "r", encoding="utf-8", errors="ignore") as f:
            pbar = tqdm(total=total, desc="AskLlmScorer")

            for line in f:
                line = line.strip()
                if not line:
                    pbar.update(1)
                    continue
                item = json.loads(line)
                item_id = normalize_id(item.get("id", ""))
                if item_id and item_id in done_ids:
                    pbar.update(1)
                    continue

                buf_items.append(item)
                buf_ids.append(item.get("id", ""))

                if len(buf_items) == self.batch_size:
                    scores = self.score_batch(buf_items)
                    records = [{"id": _id, "score": sc} for _id, sc in zip(buf_ids, scores)]
                    append_jsonl(records, output_jsonl, flush=True)
                    for _id in buf_ids:
                        nid = normalize_id(_id)
                        if nid:
                            done_ids.add(nid)
                    buf_items.clear()
                    buf_ids.clear()
                pbar.update(1)

            if buf_items:
                scores = self.score_batch(buf_items)
                records = [{"id": _id, "score": sc} for _id, sc in zip(buf_ids, scores)]
                append_jsonl(records, output_jsonl, flush=True)

            pbar.close()

    # ── public interface ──────────────────────────────────────────────

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        output_dir = Path(output_dir)
        tmp_dir = output_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        input_jsonl = tmp_dir / "ask_llm_input.jsonl"
        raw_output = tmp_dir / "ask_llm_raw_scores.jsonl"

        self._write_jsonl(data, input_jsonl)
        logger.info("Wrote %d items to temporary JSONL: %s", len(data), input_jsonl)

        self._evaluate_to_file(input_jsonl, raw_output)
        logger.info("AskLlmScorer evaluation complete -> %s", raw_output)

        scored = self._convert_results(data, raw_output)

        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Cleaned up temporary directory: %s", tmp_dir)

        n_valid = sum(1 for s in scored if s["score"] >= 0)
        logger.info(
            "Scoring done: %d total, %d valid, %d invalid",
            len(scored), n_valid, len(scored) - n_valid,
        )
        return scored

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _write_jsonl(data: list[dict], path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for i, item in enumerate(data):
                record = _sanitize_record(item, i)
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _convert_results(data: list[dict], raw_output: Path) -> list[dict]:
        sentinel = -99.0

        id_to_score: dict[int, float] = {}
        with open(raw_output, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                id_to_score[obj["id"]] = obj["score"]

        raw_scores = [id_to_score.get(i, -100.0) for i in range(len(data))]
        valid_scores = [s for s in raw_scores if s > sentinel]

        if not valid_scores:
            logger.warning("No valid scores — all set to -1")
            return [{"score": -1.0} for _ in data]

        min_s = min(valid_scores)
        max_s = max(valid_scores)
        range_s = max_s - min_s if max_s > min_s else 1.0
        logger.info(
            "Raw log-prob range: [%.4f, %.4f], normalizing to [0, 5]",
            min_s, max_s,
        )

        results: list[dict] = []
        for s in raw_scores:
            if s <= sentinel:
                results.append({"score": -1.0})
            else:
                normalized = ((s - min_s) / range_s) * 5.0
                results.append({"score": round(normalized, 4)})

        return results
