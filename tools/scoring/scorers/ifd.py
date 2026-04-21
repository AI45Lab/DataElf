"""IFD (Instruction-Following Difficulty) scorer.

IFD = conditional_ppl(output | prompt) / unconditional_ppl(output).
A higher ratio means the model relies more heavily on the instruction
context to predict the output, indicating a more instruction-dependent sample.
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

DEFAULT_TEMPLATE_NO_INPUT = (
    "<|im_start|>user\n{instruction}<|im_end|>\n<|im_start|>assistant\n"
)
DEFAULT_TEMPLATE = (
    "<|im_start|>user\n{instruction}\n{input}<|im_end|>\n<|im_start|>assistant\n"
)


class IFDScorer(BaseScorer):
    """Score data by Instruction-Following Difficulty ratio."""

    def __init__(
        self,
        model: str,
        batch_size: int = 8,
        max_length: int = 2048,
        template: str | None = None,
        template_no_input: str | None = None,
    ) -> None:
        self.model_path = model
        self.batch_size = batch_size
        self.max_length = max_length
        self.template = template or DEFAULT_TEMPLATE
        self.template_no_input = template_no_input or DEFAULT_TEMPLATE_NO_INPUT
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = ""

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("IFDScorer: loading %s", self.model_path)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path, device_map="auto", torch_dtype="auto",
        )
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
            self._model.config.pad_token_id = self._tokenizer.eos_token_id
        self._model.config.use_cache = False
        self._model.eval()
        logger.info("IFDScorer: ready on %s", self._device)

    def _pad_id_seqs(self, seqs: list[list[int]]) -> tuple[torch.Tensor, torch.Tensor]:
        if not seqs:
            empty = torch.empty((0, 0), dtype=torch.long, device=self._model.device)
            return empty, empty
        max_len = max(len(s) for s in seqs)
        pad_id = self._tokenizer.pad_token_id or 0
        input_ids = torch.full(
            (len(seqs), max_len), pad_id, dtype=torch.long, device=self._model.device,
        )
        attention_mask = torch.zeros(
            (len(seqs), max_len), dtype=torch.long, device=self._model.device,
        )
        for i, s in enumerate(seqs):
            if s:
                input_ids[i, : len(s)] = torch.tensor(s, dtype=torch.long)
                attention_mask[i, : len(s)] = 1
        return input_ids, attention_mask

    @torch.inference_mode()
    def _ppl_from_ids(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        prefix_lens: list[int] | None = None,
    ) -> list[float]:
        """Compute per-sample PPL. If prefix_lens given, mask prefix tokens."""
        if input_ids.numel() == 0:
            return []
        labels = input_ids.clone()
        if prefix_lens:
            for i, pl in enumerate(prefix_lens):
                pl = min(int(pl), labels.size(1))
                if pl > 0:
                    labels[i, :pl] = -100
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
        denom = valid.sum(dim=1)
        loss_sum = (loss_flat * valid.float()).sum(dim=1)
        avg_loss = torch.zeros_like(loss_sum)
        ok = denom > 0
        avg_loss[ok] = loss_sum[ok] / denom[ok].float()
        ppl = torch.zeros_like(avg_loss)
        ppl[ok] = torch.exp(avg_loss[ok])

        return ppl.cpu().tolist()

    @torch.inference_mode()
    def _score_batch(self, items: list[dict]) -> list[float]:
        scores = [-1.0] * len(items)

        batch_whole_ids: list[list[int]] = []
        batch_output_ids: list[list[int]] = []
        batch_prefix_lens: list[int] = []
        valid_indices: list[int] = []

        for idx, item in enumerate(items):
            instruction = item.get("instruction", "")
            _input = item.get("input", "")
            output = item.get("output", "")

            if not output:
                continue

            if _input:
                prompt = self.template.format(instruction=instruction, input=_input)
            else:
                prompt = self.template_no_input.format(instruction=instruction)

            prompt_ids = self._tokenizer(prompt, add_special_tokens=False).input_ids
            output_ids = self._tokenizer(output, add_special_tokens=False).input_ids

            whole_ids = prompt_ids + output_ids
            if len(whole_ids) > self.max_length:
                whole_ids = whole_ids[: self.max_length]

            prefix_len = min(len(prompt_ids), len(whole_ids))
            out_kept = max(0, len(whole_ids) - prefix_len)
            output_ids_kept = output_ids[:out_kept]

            bos_id = self._tokenizer.bos_token_id or self._tokenizer.eos_token_id or 0
            output_with_bos = [bos_id] + output_ids_kept

            batch_whole_ids.append(whole_ids)
            batch_output_ids.append(output_with_bos)
            batch_prefix_lens.append(prefix_len)
            valid_indices.append(idx)

        if not valid_indices:
            return scores

        whole_input_ids, whole_attn = self._pad_id_seqs(batch_whole_ids)
        out_input_ids, out_attn = self._pad_id_seqs(batch_output_ids)

        ppl_unconditional = self._ppl_from_ids(out_input_ids, out_attn)
        ppl_conditional = self._ppl_from_ids(
            whole_input_ids, whole_attn, prefix_lens=batch_prefix_lens,
        )

        for i, idx in enumerate(valid_indices):
            if ppl_unconditional[i] > 0 and ppl_conditional[i] > 0:
                scores[idx] = ppl_conditional[i] / ppl_unconditional[i]
            else:
                scores[idx] = -1.0

        return scores

    async def score(self, data: list[dict], output_dir: str | Path) -> list[dict]:
        self._ensure_model()
        raw: list[float] = []
        for i in tqdm(range(0, len(data), self.batch_size), desc="IFDScorer"):
            batch = data[i : i + self.batch_size]
            raw.extend(self._score_batch(batch))
        normed = normalize_scores(raw, higher_is_better=True)
        return [{"score": s} for s in normed]
