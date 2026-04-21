"""PIIRule — regex-based PII detector.

Pattern source:
  BerriAI/litellm — litellm/proxy/guardrails/guardrail_hooks/litellm_content_filter/patterns.json
  https://github.com/BerriAI/litellm/blob/main/litellm/proxy/guardrails/guardrail_hooks/litellm_content_filter/patterns.json

Additional patterns: CN ID card, CN phone, CN bank card.
Credit card numbers are Luhn-validated; CN ID cards use checksum validation.
"""
import re
from typing import List

from ..base import RuleBasedChecker
from ..registry import CheckerRegistry
from ._regex_scanner import load_patterns, scan_patterns
from ...schema import DataSample
from ...result import CheckResult, RiskType


def _luhn_check(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    odd_sum = sum(digits[-1::-2])
    even_sum = sum(sum(divmod(d * 2, 10)) for d in digits[-2::-2])
    return (odd_sum + even_sum) % 10 == 0


_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK   = "10X98765432"


def _cn_id_checksum(number: str) -> bool:
    n = number.upper()
    if len(n) != 18:
        return False
    total = sum(int(n[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK[total % 11] == n[17]


_VALIDATORS = {
    "luhn":            _luhn_check,
    "cn_id_checksum":  _cn_id_checksum,
}



@CheckerRegistry.register(RiskType.PII, tags=["default"])
class PIIRule(RuleBasedChecker):
    """Regex-based PII detector.

    Loads patterns from resources/patterns/pii_patterns.json.
    Credit card numbers are Luhn-validated; CN ID cards use checksum validation.
    Score 1.0 on hit, 0.0 otherwise.
    """

    def __init__(self):
        super().__init__()
        self._patterns = load_patterns("pii_patterns.json")
        if not self._patterns:
            self._log.warning("PIIRule: no patterns loaded, check pii_patterns.json.")

    def check(self, sample: DataSample) -> CheckResult:
        all_hits: List[dict] = []

        for field_name, text in sample.get_all_text_fields().items():
            for hit in scan_patterns(text, self._patterns, field_name):
                validator_name = hit.get("validator")
                if validator_name and validator_name in _VALIDATORS:
                    if not _VALIDATORS[validator_name](hit["matched"]):
                        continue
                all_hits.append(hit)

        if not all_hits:
            return CheckResult(
                checker_name=self.name,
                risk_type=self.risk_type,
                success=True,
                score=0.0,
                flagged=False,
            )

        return CheckResult(
            checker_name=self.name,
            risk_type=self.risk_type,
            success=True,
            score=1.0,
            flagged=True,
            details={
                "match_count": len(all_hits),
                "detections": [
                    {k: v for k, v in h.items() if k not in ("compiled", "validator")}
                    for h in all_hits
                ],
            },
        )
