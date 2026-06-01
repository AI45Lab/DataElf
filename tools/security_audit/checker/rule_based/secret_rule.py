"""SecretRule — regex-based credential / secret leak detector.

Pattern source:
  BerriAI/litellm — litellm/proxy/guardrails/guardrail_hooks/litellm_content_filter/patterns.json
  https://github.com/BerriAI/litellm/blob/main/litellm/proxy/guardrails/guardrail_hooks/litellm_content_filter/patterns.json

Additional patterns: OpenAI API key (sk-), PEM private key header, JWT token.
"""
from typing import List

from ..base import RuleBasedChecker
from ..registry import CheckerRegistry
from ._regex_scanner import load_patterns, scan_patterns
from ...schema import DataSample
from ...result import CheckResult, RiskType

@CheckerRegistry.register(RiskType.SECRET, tags=["default"])
class SecretRule(RuleBasedChecker):
    """Regex-based detector for credential and secret leaks in training data.

    Covers: AWS access keys, GitHub tokens, Slack tokens, generic API keys,
    OpenAI API keys, PEM private key headers, JWT tokens.

    Loads patterns from resources/patterns/secret_patterns.json.
    Score 1.0 on hit, 0.0 otherwise.
    """
    planner_metadata = {
        "description": (
            "Rule-based checker for credential and secret leakage. "
            "Scans all text fields for known token, key, private-key, JWT, "
            "and generic API credential patterns."
        ),
        "required_fields": [],
        "method": {
            "type": "rule_based",
            "pipeline": [
                "load secret regex patterns from secret_patterns.json",
                "scan every text field in the sample for credential-like matches",
                "return matched pattern evidence while omitting compiled regex objects",
                "flag the sample when at least one secret pattern is found",
            ],
        },
        "cost_profile": {
            "cost": "low",
            "latency": "low",
            "execution": "per_sample",
            "requires_llm": False,
        },
        "quality_profile": {
            "precision": "medium",
            "recall": "low",
        },
    }

    def __init__(self):
        super().__init__()
        self._patterns = load_patterns("secret_patterns.json")
        if not self._patterns:
            self._log.warning("SecretRule: no patterns loaded, check secret_patterns.json.")

    def check(self, sample: DataSample) -> CheckResult:
        all_hits: List[dict] = []

        for field_name, text in sample.get_all_text_fields().items():
            all_hits.extend(scan_patterns(text, self._patterns, field_name))

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
