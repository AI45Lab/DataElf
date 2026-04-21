# Model: leolee99/PIGuard
# https://huggingface.co/leolee99/PIGuard

from typing import Optional

from ..base import ModelBasedChecker
from ..registry import CheckerRegistry
from ...schema import DataSample
from ...result import CheckResult, RiskType

# PIGuard label: benign, injection
_INJECTION_LABEL = "injection"


@CheckerRegistry.register(RiskType.PROMPT_INJECTION, tags=["model"])
class PromptInjectionClassifier(ModelBasedChecker):
    """Prompt injection classifier based on PIGuard (DeBERTa v3-base).
    """

    def __init__(
        self,
        model_name_or_path: str = "leolee99/PIGuard",
        threshold: float = 0.5,
        max_length: int = 512,
        device: str = "auto",
    ):
        """
        Args:
            model_name_or_path: HuggingFace model ID or local path.
            threshold: injection probability score above which input is flagged.
            max_length: max token length passed to the tokenizer (PIGuard supports up to 2048).
            device: "auto", "cuda", or "cpu".
        """
        super().__init__()
        self.model_name_or_path = model_name_or_path
        self.threshold = threshold
        self.max_length = max_length
        self._device = device
        self.model = None

    def load_model(self):
        if self.model is not None:
            return
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline
            import torch

            if self._device == "auto":
                device = 0 if torch.cuda.is_available() else -1
            elif self._device == "cuda":
                device = 0
            else:
                device = -1

            self._log.info(f"PromptInjectionClassifier: loading model '{self.model_name_or_path}' ...")
            tokenizer = AutoTokenizer.from_pretrained(
                self.model_name_or_path,
                model_max_length=self.max_length,
            )
            model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name_or_path,
                trust_remote_code=True,
            )
            self.model = pipeline(
                "text-classification",
                model=model,
                tokenizer=tokenizer,
                device=device,
                truncation=True,
            )
            self._log.info("PromptInjectionClassifier: model loaded.")
        except Exception as e:
            raise RuntimeError(f"PromptInjectionClassifier: failed to load model '{self.model_name_or_path}': {e}") from e

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        try:
            self.load_model()
        except RuntimeError as e:
            self._log.warning(f"{e} on sample {sample.id}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        # Collect system + user message text only; injections originate from input
        input_parts = []
        for m in sample.messages:
            if m.role in ("system", "user"):
                input_parts.extend(m.get_text_parts())

        text = "\n".join(input_parts).strip()
        if not text:
            return CheckResult(**base, success=True, score=0.0, flagged=False)

        try:
            result = self.model(text)
            # pipeline returns [{"label": ..., "score": ...}] for a single string
            if isinstance(result, list) and result and isinstance(result[0], list):
                result = result[0]  # unwrap batch dimension

            injection_score = 0.0
            for item in result:
                if item["label"] == _INJECTION_LABEL:
                    injection_score = item["score"]
                    break

            flagged = injection_score >= self.threshold
            evidence: Optional[str] = text[:200] if flagged else None

            return CheckResult(
                **base,
                success=True,
                score=round(injection_score, 4),
                flagged=flagged,
                details={
                    "injection_score": round(injection_score, 4),
                    "evidence": evidence,
                },
            )
        except Exception as e:
            self._log.warning(f"PromptInjectionClassifier: inference failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})
