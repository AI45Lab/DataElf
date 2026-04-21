from __future__ import annotations

import logging

import numpy as np
import torch
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

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


def format_sample(item: dict) -> str:
    instruction = _as_text(item.get("instruction", ""))
    inp = _as_text(item.get("input", ""))
    output = _as_text(item.get("output", ""))

    parts = [f"### Instruction:\n{instruction}"]
    if inp.strip():
        parts.append(f"### Input:\n{inp}")
    parts.append(f"### Response:\n{output}")
    return "\n\n".join(parts)


@torch.no_grad()
def extract_embeddings(
    data: list[dict],
    model_name_or_path: str,
    batch_size: int = 64,
    max_length: int = 1024,
    device: str = "cuda",
    dtype: str = "bfloat16",
) -> np.ndarray:
    torch_dtype = TORCH_DTYPE_MAP[dtype]

    tokenizer = AutoTokenizer.from_pretrained(
        model_name_or_path,
        model_max_length=max_length,
        padding_side="left",
        use_fast=False,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        model_name_or_path,
        torch_dtype=torch_dtype,
        device_map=device,
    )
    model.eval()

    texts = [format_sample(item) for item in data]
    all_embeddings: list[np.ndarray] = []

    for start in tqdm(range(0, len(texts), batch_size), desc="Extracting embeddings"):
        batch_texts = texts[start : start + batch_size]
        encodings = tokenizer(
            batch_texts,
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="pt",
        ).to(model.device)

        outputs = model(
            input_ids=encodings["input_ids"],
            attention_mask=encodings["attention_mask"],
            output_hidden_states=True,
        )

        last_hidden = outputs.hidden_states[-1]
        attention_mask = encodings["attention_mask"]

        if tokenizer.padding_side == "left":
            embeddings = last_hidden[:, -1, :]
        else:
            seq_lengths = attention_mask.sum(dim=1) - 1
            embeddings = last_hidden[torch.arange(last_hidden.size(0)), seq_lengths]

        all_embeddings.append(embeddings.float().cpu().numpy())

    return np.concatenate(all_embeddings, axis=0)
