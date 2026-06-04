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


class _DetoxifyPredictor:
    def __init__(self, model, tokenizer, class_names, device):
        self.model = model
        self.tokenizer = tokenizer
        self.class_names = class_names
        self.device = device
        self.model.to(self.device)

    def predict(self, text):
        import torch

        self.model.eval()
        with torch.no_grad():
            inputs = self.tokenizer(
                text, return_tensors="pt", truncation=True, padding=True
            ).to(self.device)
            out = self.model(**inputs)[0]
            scores = torch.sigmoid(out).cpu().detach().numpy()

        results = {}
        for i, class_name in enumerate(self.class_names):
            if isinstance(text, str):
                results[class_name] = scores[0][i]
            else:
                results[class_name] = [
                    scores[example_i][i].tolist()
                    for example_i in range(len(scores))
                ]
        return results


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
        checkpoint: Optional[str] = None,
        huggingface_config_path: Optional[str] = None,
    ):
        """
        Args:
            model_type: detoxify model variant — "original", "unbiased", or "multilingual".
            threshold: score above which the text is flagged as toxic.
            device: "auto", "cuda", or "cpu".
            checkpoint: optional local Detoxify checkpoint path for offline loading.
            huggingface_config_path: optional local HF config/tokenizer directory for offline loading.
        """
        super().__init__()
        self.model_type = model_type
        self.threshold = threshold
        self._device = device
        self.checkpoint = checkpoint
        self.huggingface_config_path = huggingface_config_path
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
            if self.checkpoint is not None:
                self.model = self._load_local_detoxify_model(device)
            else:
                self.model = Detoxify(self.model_type, device=device)
            self._log.info("ToxicityClassifier: model loaded.")
        except ImportError:
            raise ImportError(
                "ToxicityClassifier requires detoxify. "
                "Install with: pip install detoxify"
            )
        except Exception as e:
            raise RuntimeError(f"ToxicityClassifier: failed to load model '{self.model_type}': {e}") from e

    def _load_local_detoxify_model(self, device: str):
        import torch
        import transformers

        loaded = torch.load(self.checkpoint, map_location=device)
        if "config" not in loaded or "state_dict" not in loaded:
            raise ValueError(
                "Detoxify checkpoint needs to contain both config and state_dict"
            )

        class_names = loaded["config"]["dataset"]["args"]["classes"]
        rename = {
            "toxic": "toxicity",
            "identity_hate": "identity_attack",
            "severe_toxic": "severe_toxicity",
        }
        class_names = [rename.get(class_name, class_name) for class_name in class_names]

        arch_args = loaded["config"]["arch"]["args"]
        model_class = getattr(transformers, arch_args["model_name"])
        tokenizer_class = getattr(transformers, arch_args["tokenizer_name"])
        model_source = self.huggingface_config_path or arch_args["model_type"]
        local_files_only = self.huggingface_config_path is not None

        config = model_class.config_class.from_pretrained(
            model_source,
            num_labels=arch_args["num_classes"],
            local_files_only=local_files_only,
        )
        model = model_class.from_pretrained(
            pretrained_model_name_or_path=None,
            config=config,
            state_dict=loaded["state_dict"],
            local_files_only=local_files_only,
        )
        tokenizer = tokenizer_class.from_pretrained(
            model_source,
            local_files_only=local_files_only,
        )

        return _DetoxifyPredictor(model, tokenizer, class_names, device)

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
