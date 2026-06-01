# Model: unitaryai/detoxify (unbiased)
# https://github.com/unitaryai/detoxify

from typing import Dict, Optional

from ..base import ModelBasedChecker
from ..registry import CheckerRegistry
from ...schema import DataSample, DataFormat
from ...result import CheckResult, RiskType

# detoxify label keys
TOXICITY_LABELS = [
    "toxicity", "severe_toxicity", "obscene",
    "threat", "insult", "identity_attack", "sexual_explicit",
]


@CheckerRegistry.register(RiskType.TOXICITY, tags=["model"])
class ToxicityClassifier(ModelBasedChecker):
    """Toxicity classifier based on the detoxify library (Unitary).
    """
    planner_metadata = {
        "description": (
            "Model-based checker for toxic language. "
            "Uses Detoxify to score toxicity, severe toxicity, obscenity, threats, "
            "insults, identity attacks, and sexual explicitness across sample text fields."
        ),
        "required_fields": [],
        "method": {
            "type": "model_based",
            "pipeline": [
                "lazy-load the configured Detoxify model variant",
                "score each text field except DPO rejected_response",
                "select the highest toxicity label score across fields",
                "flag when the highest score meets the configured threshold",
            ],
        },
        "cost_profile": {
            "cost": "medium",
            "latency": "medium",
            "execution": "per_sample",
            "requires_llm": False,
        },
        "quality_profile": {
            "precision": "medium",
            "recall": "medium",
        },
    }

    def __init__(
        self,
        model_type: str = "unbiased",
        threshold: float = 0.5,
        device: str = "auto",
    ):
        """
        Args:
            model_type: detoxify model variant — "original", "unbiased", or "multilingual".
            threshold: score above which the text is flagged as toxic.
            device: "auto", "cuda", or "cpu".
        """
        super().__init__()
        self.model_type = model_type
        self.threshold = threshold
        self._device = device
        self.model = None

    def load_model(self):
        if self.model is not None:
            return
        try:
            from detoxify import Detoxify
            import torch

            if self._device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            else:
                device = self._device

            self._log.info(f"ToxicityClassifier: loading detoxify model '{self.model_type}' on {device} ...")
            self.model = Detoxify(self.model_type, device=device)
            self._log.info("ToxicityClassifier: model loaded.")
        except ImportError:
            raise ImportError(
                "ToxicityClassifier requires detoxify. "
                "Install with: pip install detoxify"
            )
        except Exception as e:
            raise RuntimeError(f"ToxicityClassifier: failed to load model '{self.model_type}': {e}") from e

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        try:
            self.load_model()
        except (ImportError, RuntimeError) as e:
            self._log.warning(f"{e} on sample {sample.id}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        try:
            worst_score = 0.0
            worst_field = None
            worst_details: Dict = {}

            for field_name, text in sample.get_all_text_fields().items():
                # For DPO rejected_response, relax threshold (negative example)
                if (sample.dataset_type == DataFormat.DPO
                        and field_name == "rejected_response"):
                    continue

                raw_scores = self.model.predict(text)
                # Convert numpy.float32 -> Python float to ensure JSON serializable
                scores = {k: float(v) for k, v in raw_scores.items()}
                max_label = max(TOXICITY_LABELS, key=lambda k: scores.get(k, 0.0))
                max_score = scores.get(max_label, 0.0)

                if max_score > worst_score:
                    worst_score = max_score
                    worst_field = field_name
                    worst_details = {
                        k: round(v, 4) for k, v in scores.items()
                        if k in TOXICITY_LABELS
                    }

            flagged = worst_score >= self.threshold

            return CheckResult(
                **base,
                success=True,
                score=round(worst_score, 4),
                flagged=flagged,
                details=worst_details,
            )
        except Exception as e:
            self._log.warning(f"ToxicityClassifier: inference failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})
