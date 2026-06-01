from typing import List

from ..base import RuleBasedChecker
from ..registry import CheckerRegistry
from ._keyword_scanner import load_wordlist, scan_keywords
from ...schema import DataSample
from ...result import CheckResult, RiskType


@CheckerRegistry.register(RiskType.HARMFUL, tags=["default"])
class HarmfulKeywordRule(RuleBasedChecker):
    """Harmful content detector based on keyword wordlist.

    Loads harmful_keywords.txt; scans all text fields.
    Score 1.0 on hit, 0.0 otherwise.
    using field_name + dataset_type.
    """
    planner_metadata = {
        "description": (
            "Rule-based checker for harmful content. "
            "Scans all text fields against English and Chinese harmful-content keyword wordlists."
        ),
        "required_fields": [],
        "method": {
            "type": "rule_based",
            "pipeline": [
                "load harmful-content keyword wordlists",
                "scan every text field in the sample for keyword hits",
                "collect field-level keyword evidence",
                "flag the sample when at least one harmful keyword is found",
            ],
        },
        "cost_profile": {
            "cost": "low",
            "latency": "low",
            "execution": "per_sample",
            "requires_llm": False,
        },
        "quality_profile": {
            "precision": "low",
            "recall": "low",
        },
    }

    def __init__(self):
        super().__init__()
        self._keywords = load_wordlist(
            "harmful_hurtlex_en.txt",
            "harmful_hurtlex_zh.txt",
        )
        if not self._keywords:
            self._log.warning("HarmfulKeywordRule: no keywords loaded, check wordlist files.")

    def check(self, sample: DataSample) -> CheckResult:
        all_hits: List[dict] = []

        for field_name, text in sample.get_all_text_fields().items():
            all_hits.extend(scan_keywords(text, self._keywords, field_name))

        if not all_hits:
            return CheckResult(
                checker_name=self.name,
                risk_type=self.risk_type,
                success=True,
                score=0.0,
                flagged=False,
            )

        worst = all_hits[0]

        return CheckResult(
            checker_name=self.name,
            risk_type=self.risk_type,
            success=True,
            score=1.0,
            flagged=True,
            details={
                "match_count": len(all_hits),
                "detections": [
                    {"field_name": h["field_name"], "keyword": h["keyword"]}
                    for h in all_hits
                ],
            },
        )
