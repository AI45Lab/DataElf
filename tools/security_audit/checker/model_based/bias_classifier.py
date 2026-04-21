# Model: cirimus/modernbert-large-bias-type-classifier
# https://huggingface.co/cirimus/modernbert-large-bias-type-classifier

from typing import Dict, List

from ..base import ModelBasedChecker
from ..registry import CheckerRegistry
from ...schema import DataSample, DataFormat
from ...result import CheckResult, RiskType


# 11 bias categories from the model
BIAS_LABELS = [
    "racial", "religious", "gender", "age", "nationality",
    "sexuality", "socioeconomic", "political", "disability",
    "physical", "other",
]


@CheckerRegistry.register(RiskType.BIAS, tags=["model"])
class BiasClassifier(ModelBasedChecker):
    """Multi-label bias type classifier based on ModernBERT-large.

    Detects 11 bias categories: racial, religious, gender, age, nationality,
    sexuality, socioeconomic, political, disability, physical, other.
    """

    def __init__(
        self,
        model_name_or_path: str = "cirimus/modernbert-large-bias-type-classifier",
        threshold: float = 0.5,
        max_length: int = 512,
        device: str = "auto",
    ):
        """
        Args:
            model_name_or_path: HuggingFace model ID or local path.
            threshold: per-category score above which text is flagged as biased.
            max_length: max token length for truncation.
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
            from transformers import pipeline
            import torch

            if self._device == "auto":
                device = 0 if torch.cuda.is_available() else -1
            elif self._device == "cuda":
                device = 0
            else:
                device = -1

            self._log.info(f"BiasClassifier: loading model '{self.model_name_or_path}' ...")
            self.model = pipeline(
                "text-classification",
                model=self.model_name_or_path,
                top_k=None,  # return all label scores
                device=device,
                truncation=True,
                max_length=self.max_length,
            )
            self._log.info("BiasClassifier: model loaded.")
        except Exception as e:
            raise RuntimeError(f"BiasClassifier: failed to load model '{self.model_name_or_path}': {e}") from e

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        try:
            self.load_model()
        except RuntimeError as e:
            self._log.warning(f"{e} on sample {sample.id}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        try:
            worst_score = 0.0
            worst_field = None
            worst_categories: List[Dict] = []

            for field_name, text in sample.get_all_text_fields().items():
                if (sample.dataset_type == DataFormat.DPO
                        and field_name == "rejected_response"):
                    continue

                if not text.strip():
                    continue

                # pipeline returns list of {label, score} dicts for all categories
                scores = self.model(text[:self.max_length * 4])
                if isinstance(scores, list) and scores and isinstance(scores[0], list):
                    scores = scores[0]  # unwrap batch dimension

                # Find max bias score across categories
                field_max = 0.0
                field_cats = []
                for item in scores:
                    label = item["label"]
                    score = item["score"]
                    if score >= self.threshold:
                        field_cats.append({"category": label, "score": round(score, 4)})
                    if score > field_max:
                        field_max = score

                if field_max > worst_score:
                    worst_score = field_max
                    worst_field = field_name
                    worst_categories = field_cats

            flagged = worst_score >= self.threshold
            evidence = None
            if flagged and worst_categories:
                parts = [f"{c['category']}={c['score']}" for c in worst_categories]
                evidence = f"bias detected: {', '.join(parts)}"

            return CheckResult(
                **base,
                success=True,
                score=round(worst_score, 4),
                flagged=flagged,
                details={
                    "bias_score": round(worst_score, 4),
                    "flagged_categories": worst_categories,
                    "field_name": worst_field,
                    "evidence": evidence,
                },
            )
        except Exception as e:
            self._log.warning(f"BiasClassifier: inference failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})
