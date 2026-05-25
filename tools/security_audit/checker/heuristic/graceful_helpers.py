from __future__ import annotations

from typing import Dict, List, Optional, Union

import torch
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

IGNORE_INDEX = -100


def dct_1d(x: torch.Tensor) -> torch.Tensor:
    n = x.shape[-1]
    x = x.reshape(-1, n)
    device = x.device
    k = torch.arange(n, device=device).float()
    n_idx = torch.arange(n, device=device).float()
    cos_part = torch.cos(torch.pi / n * (n_idx + 0.5)[:, None] * k[None, :])
    result = 2.0 * torch.matmul(x, cos_part)
    return result.reshape(*x.shape[:-1], n)


def dct_2d(x: torch.Tensor) -> torch.Tensor:
    x = dct_1d(x)
    x = dct_1d(x.transpose(-1, -2)).transpose(-1, -2)
    return x


def casual_collate_fn(data: List[tuple]) -> Dict[str, List]:
    contexts = []
    targets = []
    poison_labels = []
    for context, target, poison_label in data:
        contexts.append(context)
        targets.append(target)
        poison_labels.append(poison_label)
    return {
        "context": contexts,
        "target": targets,
        "poison_label": poison_labels,
    }


def get_casual_dataloader(
    dataset: Union[List, torch.utils.data.Dataset],
    batch_size: Optional[int] = 4,
    shuffle: Optional[bool] = True,
):
    return DataLoader(dataset=dataset, batch_size=batch_size, shuffle=shuffle, collate_fn=casual_collate_fn)


class CasualLLMVictim:
    """Minimal Causal LM victim wrapper for GraCeFul-style gradient extraction."""

    def __init__(
        self,
        device: Optional[str] = "gpu",
        model: Optional[str] = "llama",
        path: Optional[str] = None,
        max_len: Optional[int] = 4096,
        local_files_only: bool = False,
        **kwargs,
    ):
        if not path:
            raise ValueError("victim model path is required")
        self.device = torch.device(
            "cuda" if device in {"gpu", "cuda"} and torch.cuda.is_available() else "cpu"
        )
        self.model_type = model
        self.local_files_only = local_files_only
        self.model_config = AutoConfig.from_pretrained(path, local_files_only=local_files_only)
        self.llm = AutoModelForCausalLM.from_pretrained(
            path,
            config=self.model_config,
            trust_remote_code=True,
            device_map="auto" if self.device.type == "cuda" else None,
            local_files_only=local_files_only,
        )
        if self.device.type != "cuda":
            self.llm = self.llm.to(self.device)
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=local_files_only)
        if self.model_type == "llama":
            pad_token = self.tokenizer.unk_token or self.tokenizer.eos_token
            self.llm.config.pad_token_id = self.tokenizer.convert_tokens_to_ids(pad_token)
            self.tokenizer.pad_token = pad_token
            self.tokenizer.padding_side = "left"
        self.max_len = max_len

    def to(self, device):
        self.llm = self.llm.to(device)
        return self

    def train(self, mode=True):
        self.llm.train(mode)
        return self

    def zero_grad(self, set_to_none=True):
        self.llm.zero_grad(set_to_none=set_to_none)

    def named_parameters(self, *args, **kwargs):
        return self.llm.named_parameters(*args, **kwargs)

    def get_target_params(self, target_para: str) -> List[torch.nn.Parameter]:
        """Get target parameters by name, handling tied weights.

        ``named_parameters()`` deduplicates tied tensors, so ``lm_head.weight``
        may be absent when it is tied to the embedding layer.  This method
        falls back to direct module attribute access.
        """
        params = [p for n, p in self.named_parameters() if target_para in n]
        if params:
            return params
        # Tied-weight fallback: walk submodules for an exact match
        for name, module in self.llm.named_modules():
            if target_para.rsplit(".", 1)[0] in name:
                attr = target_para.rsplit(".", 1)[-1]
                param = getattr(module, attr, None)
                if param is not None and isinstance(param, torch.nn.Parameter):
                    return [param]
        return []

    def forward(self, inputs, labels=None, attentionMask=None):
        if labels is None:
            return self.llm.forward(
                input_ids=inputs,
                output_hidden_states=True,
                attention_mask=attentionMask,
            )
        return self.llm.forward(
            input_ids=inputs,
            labels=labels,
            output_hidden_states=True,
            attention_mask=attentionMask,
        )

    def train_process(self, batch):
        contexts, targets = batch["context"], batch["target"]
        targets = ["; ".join(target) if isinstance(target, list) else target for target in targets]
        context_ids = [
            self.tokenizer.encode(context, max_length=self.max_len, truncation=True, padding=False)
            for context in contexts
        ]
        target_ids = [
            self.tokenizer.encode(target, max_length=self.max_len, truncation=True, add_special_tokens=False, padding=False)
            for target in targets
        ]
        input_ids = [
            context_id + target_id + [self.tokenizer.eos_token_id]
            for context_id, target_id in zip(context_ids, target_ids)
        ]

        context_lens = [len(context_id) for context_id in context_ids]
        input_lens = [len(input_id) for input_id in input_ids]
        input_batch = []
        labels = []
        for input_len, input_id, context_len in sorted(
            zip(input_lens, input_ids, context_lens),
            key=lambda x: -x[0],
        ):
            input_tensor = torch.LongTensor(input_id)
            label = input_tensor.clone()
            label[:context_len] = IGNORE_INDEX
            input_batch.append(input_tensor)
            labels.append(label)
        input_batch = pad_sequence(
            input_batch,
            batch_first=True,
            padding_value=self.tokenizer.pad_token_id,
        ).to(self.device)
        labels = pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX).to(self.device)
        attention_mask = input_batch.ne(self.tokenizer.pad_token_id).to(self.device)

        return input_batch, labels, attention_mask

    def process(self, batch, train=True):
        if train:
            return self.train_process(batch)
        return self.tokenizer(
            batch["context"],
            max_length=self.max_len,
            truncation=True,
            padding=False,
            return_tensors="pt",
        ).to(self.device)


def load_victim(config: Dict) -> CasualLLMVictim:
    victim_type = config.get("type", "casual")
    if victim_type != "casual":
        raise ValueError(f"unsupported victim type: {victim_type}")
    cfg = dict(config)
    cfg.pop("type", None)
    return CasualLLMVictim(**cfg)
