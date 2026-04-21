# Model: meta-llama/Llama-Guard-3-8B
# https://huggingface.co/meta-llama/Llama-Guard-3-8B

from typing import Dict, List, Optional

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

from ..base import ModelBasedChecker
from ..registry import CheckerRegistry
from ...schema import DataSample, DataFormat
from ...result import CheckResult, RiskType

# Llama Guard 3 hazard categories (MLCommons taxonomy)
HAZARD_CATEGORIES = {
    "S1": "Violent Crimes",
    "S2": "Non-Violent Crimes",
    "S3": "Sex-Related Crimes",
    "S4": "Child Sexual Exploitation",
    "S5": "Defamation",
    "S6": "Specialized Advice",
    "S7": "Privacy",
    "S8": "Intellectual Property",
    "S9": "Indiscriminate Weapons",
    "S10": "Hate",
    "S11": "Suicide & Self-Harm",
    "S12": "Sexual Content",
    "S13": "Elections",
    "S14": "Code Interpreter Abuse",
}


@CheckerRegistry.register(RiskType.HARMFUL, tags=["model"])
class HarmfulContentClassifier(ModelBasedChecker):
    """Harmful content classifier based on Meta Llama-Guard-3-8B.

    Uses Llama Guard's chat-template-based moderation to classify whether
    a conversation contains harmful content across 14 hazard categories.
    """

    def __init__(
        self,
        model_name_or_path: str = "meta-llama/Llama-Guard-3-8B",
        device: str = "auto",
        dtype: str = "bfloat16",
        threshold: float = 0.5,
    ):
        """
        Args:
            model_name_or_path: HuggingFace model ID or local path to Llama Guard.
            device: "auto", "cuda", "cpu".
            dtype: torch dtype string, "bfloat16" or "float16".
            threshold: unsafe probability above which the sample is flagged.
        """
        super().__init__()
        self.model_name_or_path = model_name_or_path
        self.device = device
        self.dtype = getattr(torch, dtype, torch.bfloat16)
        self.threshold = threshold
        self._tokenizer = None
        self.model = None
        self._safe_token_id = None
        self._unsafe_token_id = None

    def load_model(self):
        if self.model is not None:
            return
        try:
            self._log.info("HarmfulContentClassifier: loading Llama Guard model ...")
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path)
            if self.device in ("auto", "cuda"):
                # Use device_map for GPU (handles multi-GPU sharding)
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.model_name_or_path,
                    torch_dtype=self.dtype,
                    device_map=self.device,
                )
            else:
                # CPU: load without device_map to avoid meta tensor issues
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.model_name_or_path,
                    torch_dtype=self.dtype,
                ).to(self.device)
            self.model.eval()
            # Pre-compute token IDs for "safe" and "unsafe"
            self._safe_token_id = self._tokenizer.convert_tokens_to_ids("safe")
            self._unsafe_token_id = self._tokenizer.convert_tokens_to_ids("unsafe")
            self._log.info("HarmfulContentClassifier: model loaded.")
        except Exception as e:
            raise RuntimeError(f"HarmfulContentClassifier: failed to load model '{self.model_name_or_path}': {e}") from e

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        try:
            self.load_model()
        except RuntimeError as e:
            self._log.warning(f"{e}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        conversation = self._build_conversation(sample)
        if not conversation:
            return self._safe_result(0.0)

        try:
            unsafe_prob, label, categories = self._moderate(conversation)
            flagged = unsafe_prob >= self.threshold

            if label == "unsafe" and categories:
                cat_names = [HAZARD_CATEGORIES.get(c, c) for c in categories]
                return CheckResult(
                    **base,
                    success=True,
                    score=round(unsafe_prob, 4),
                    flagged=flagged,
                    field_name="messages",
                    evidence=f"unsafe({unsafe_prob:.4f}): {', '.join(f'{c}({n})' for c, n in zip(categories, cat_names))}",
                    details={
                        "label": label,
                        "unsafe_prob": round(unsafe_prob, 4),
                        "categories": categories,
                        "category_names": cat_names,
                    },
                )

            return self._safe_result(unsafe_prob)
        except Exception as e:
            self._log.warning(f"HarmfulContentClassifier: inference failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})

    def _build_conversation(self, sample: DataSample) -> List[Dict[str, str]]:
        """Convert DataSample messages into Llama Guard conversation format."""
        conversation = []
        for msg in sample.messages:
            texts = msg.get_text_parts()
            if texts:
                conversation.append({
                    "role": msg.role,
                    "content": " ".join(texts),
                })

        # Append response if present (SFT / RL)
        if sample.response and sample.response.content:
            conversation.append({
                "role": "assistant",
                "content": sample.response.content,
            })

        # For DPO, check chosen_response (rejected is expected to be bad)
        if sample.dataset_type == DataFormat.DPO:
            if sample.chosen_response and sample.chosen_response.content:
                conversation.append({
                    "role": "assistant",
                    "content": sample.chosen_response.content,
                })

        return conversation

    def _moderate(self, conversation: List[Dict[str, str]]) -> tuple:
        """Run Llama Guard on a conversation.

        Returns:
            (unsafe_prob, label, categories) where unsafe_prob is the
            softmax probability of the "unsafe" token at the first
            generated position.
        """
        inputs = self._tokenizer.apply_chat_template(
            conversation, return_tensors="pt"
        )
        if hasattr(inputs, "input_ids"):
            input_ids = inputs.input_ids.to(self.model.device)
        else:
            input_ids = inputs.to(self.model.device)

        with torch.no_grad():
            # Step 1: the model always generates a newline token first,
            # then "safe" or "unsafe". Append the newline token to the
            # prompt so the next-token logits correspond to safe/unsafe.
            newline_id = 271  # "\n\n" token in Llama Guard
            extended = torch.cat([
                input_ids,
                torch.tensor([[newline_id]], device=input_ids.device),
            ], dim=1)
            outputs = self.model(extended)
            last_logits = outputs.logits[0, -1, :]

            # Compute P(unsafe) via softmax over the safe/unsafe pair
            pair_logits = torch.tensor([
                last_logits[self._safe_token_id],
                last_logits[self._unsafe_token_id],
            ])
            probs = torch.softmax(pair_logits.float(), dim=0)
            unsafe_prob = probs[1].item()

            # Step 2: generate full output to get category labels
            output = self.model.generate(
                input_ids=input_ids,
                max_new_tokens=100,
                pad_token_id=0,
            )

        prompt_len = input_ids.shape[-1]
        output_text = self._tokenizer.decode(
            output[0][prompt_len:], skip_special_tokens=True
        )
        label, categories = self._parse_output(output_text)
        return unsafe_prob, label, categories

    @staticmethod
    def _parse_output(output_text: str) -> tuple:
        """Parse Llama Guard output into (label, categories)."""
        lines = output_text.strip().split("\n")
        label = lines[0].strip().lower()
        categories = []
        if label == "unsafe" and len(lines) > 1:
            categories = [c.strip() for c in lines[1].split(",") if c.strip()]
        return label, categories

    def _safe_result(self, unsafe_prob: float = 0.0) -> CheckResult:
        return CheckResult(
            checker_name=self.name,
            risk_type=self.risk_type,
            success=True,
            score=round(unsafe_prob, 4),
            flagged=False,
            details={"label": "safe", "unsafe_prob": round(unsafe_prob, 4)},
        )
