import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, List, Optional

from ..schema import DataSample
from ..result import CheckResult, RiskType


class CheckerType(str, Enum):
    RULE_BASED  = "rule_based"
    LLM_JUDGE   = "llm_judge"
    HEURISTIC   = "heuristic"
    MODEL_BASED = "model_based"


class BaseChecker(ABC):
    """Abstract base class for all checkers."""
    name: str = ""
    checker_type: CheckerType
    risk_type: RiskType
    supported_formats: Optional[List[str]] = None  # None means all dataset types

    def __init__(self, log: Optional[Any] = None, **kwargs):
        self._log = log or logging.getLogger(type(self).__module__)

    @abstractmethod
    def check(self, sample: DataSample) -> CheckResult:
        """Check a single sample and return the result."""
        ...


class RuleBasedChecker(BaseChecker):
    """Rule-based checker.

    Deterministic symbolic matching and regex logic; no model inference, low cost.
    Suitable for: PII detection, keyword matching, format anomaly detection.
    """
    checker_type = CheckerType.RULE_BASED


class LLMJudgeChecker(BaseChecker):
    """LLM-as-a-Judge checker.

    Uses a powerful LLM for complex semantic reasoning via prompt engineering.
    Suitable for: toxicity/bias/harmful content deep analysis, alignment evaluation.
    """
    checker_type = CheckerType.LLM_JUDGE
    llm_model: str = ""        # set by Executor from config.llm.model

    def __init__(self, llm=None, **kwargs):
        super().__init__(**kwargs)
        self.llm = llm


class HeuristicChecker(BaseChecker):
    """Statistical heuristic checker.

    Detects risks via distribution anomalies and statistical patterns.
    Suitable for: data poisoning, perplexity anomalies, frequency outliers.
    """
    checker_type = CheckerType.HEURISTIC

    def check_batch(self, samples: List[DataSample]) -> List[CheckResult]:
        """Batch check interface for global statistics.

        Default: calls check() per sample. Override for true batch processing.
        """
        return [self.check(s) for s in samples]


class ModelBasedChecker(BaseChecker):
    """Model-based checker.

    Uses a fine-tuned classification/detection model for semantic judgement.
    Suitable for: toxicity classification (detoxify), NER-PII, bias classifiers.
    """
    checker_type = CheckerType.MODEL_BASED
    model = None

    def load_model(self):
        """Lazy-load the model; called on first check."""
        raise NotImplementedError
