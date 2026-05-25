# Model: Microsoft Presidio (regex + spaCy NER)
# https://github.com/microsoft/presidio

import os
from typing import Dict, List, Optional, Tuple

from ..base import ModelBasedChecker
from ..registry import CheckerRegistry
from ...schema import DataSample
from ...result import CheckResult, RiskType

@CheckerRegistry.register(RiskType.PII, tags=["model"])
class PIINERDetector(ModelBasedChecker):
    """PII detector based on Microsoft Presidio (regex + spaCy NER).
    """

    def __init__(
        self,
        language: str = "en",
        score_threshold: float = 0.5,
        entities: Optional[List[str]] = None,
    ):
        """
        Args:
            language: analysis language, default "en". "zh" requires zh_core_web_sm.
            score_threshold: Presidio confidence threshold; results below this are ignored.
            entities: list of entity types to detect; None means all supported types.
        """
        try:
            import spacy
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            from presidio_anonymizer.entities import OperatorConfig
        except ImportError:
            raise ImportError(
                "PIINERDetector requires presidio-analyzer and presidio-anonymizer. "
                "Install with: pip install presidio-analyzer presidio-anonymizer"
            )

        super().__init__()
        try:
            spacy_model = "en_core_web_lg" if language == "en" else f"{language}_core_web_sm"
            if not spacy.util.is_package(spacy_model):
                if os.environ.get("DATAELF_OFFLINE_MODE") == "1":
                    raise RuntimeError(
                        f"spaCy model '{spacy_model}' is required in offline mode; "
                        "install it before running PIINERDetector."
                    )
                self._log.info(f"spaCy model '{spacy_model}' not found, downloading ...")
                spacy.cli.download(spacy_model)

            self.model = AnalyzerEngine()
            self.anonymizer = AnonymizerEngine()
            self.operators = {
                "DEFAULT": OperatorConfig("replace", {"new_value": "<REDACTED>"}),
            }
        except Exception as e:
            raise RuntimeError(f"PIINERDetector: failed to initialize presidio engine: {e}") from e

        self.language = language
        self.score_threshold = score_threshold
        self.entities = entities

    def load_model(self):
        pass

    def check(self, sample: DataSample) -> CheckResult:
        field_hits: Dict[str, Tuple[str, list]] = {}
        all_detections: List[dict] = []

        for field_name, text in sample.get_all_text_fields().items():
            results = self._analyze_text(text)
            if not results:
                continue
            field_hits[field_name] = (text, results)
            for r in results:
                all_detections.append({
                    "field_name": field_name,
                    "entity_type": r.entity_type,
                    "score": round(r.score, 3),
                    "start": r.start,
                    "end": r.end,
                })

        if not all_detections:
            return CheckResult(
                checker_name=self.name,
                risk_type=self.risk_type,
                success=True,
                score=0.0,
                flagged=False,
            )

        worst = max(all_detections, key=lambda d: d["score"])
        worst_field = worst["field_name"]
        worst_text, worst_results = field_hits[worst_field]
        evidence = self._sanitize(worst_text, worst_results)

        return CheckResult(
            checker_name=self.name,
            risk_type=self.risk_type,
            success=True,
            score=worst["score"],
            flagged=True,
            details={
                "match_count": len(all_detections),
                "detections": all_detections,
                "field_name": worst_field,
                "evidence": evidence,
            },
        )

    def get_supported_entities(self) -> List[str]:
        return self.model.get_supported_entities(language=self.language)

    def _analyze_text(self, text: str) -> list:
        if not text:
            return []
        try:
            results = self.model.analyze(
                text=text,
                entities=self.entities,
                language=self.language,
            )
            return [r for r in results if r.score >= self.score_threshold]
        except Exception as e:
            self._log.warning(f"PIINERDetector: presidio analysis failed: {e}")
            return []

    def _sanitize(self, text: str, analyzer_results: list) -> str:
        try:
            result = self.anonymizer.anonymize(
                text=text,
                analyzer_results=analyzer_results,
                operators=self.operators,
            )
            return result.text or ""
        except Exception as e:
            self._log.warning(f"PIINERDetector: anonymization failed: {e}")
            return text
