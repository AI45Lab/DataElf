from typing import List

from ..base import RuleBasedChecker
from ..registry import CheckerRegistry
from ._keyword_scanner import load_wordlist, scan_keywords
from ...schema import DataSample
from ...result import CheckResult, RiskType


@CheckerRegistry.register(RiskType.BIAS, tags=["default"])
class BiasKeywordRule(RuleBasedChecker):
    """Bias detector based on keyword wordlists.

    Loads bias_hurtlex_en.txt and bias_hurtlex_zh.txt; scans all text fields.
    Score 1.0 on hit, 0.0 otherwise.
    """

    def __init__(self):
        super().__init__()
        self._keywords = load_wordlist(
            "bias_hurtlex_en.txt",
            "bias_hurtlex_zh.txt",
        )
        if not self._keywords:
            self._log.warning("BiasKeywordRule: no keywords loaded, check wordlist files.")

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
