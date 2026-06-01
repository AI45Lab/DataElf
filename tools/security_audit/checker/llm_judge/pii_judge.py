"""
PIILLMJudge: LLM-as-a-Judge checker for PII detection using a two-step pipeline.

References: https://github.com/confident-ai/deepeval/blob/main/deepeval/metrics/pii_leakage/template.py
"""
import math

from ..base import LLMJudgeChecker
from ..registry import CheckerRegistry
from ...schema import DataSample
from ...result import CheckResult, RiskType
from .helper import format_content, call_llm_extract_list
from exceptions import LLMError

_EXTRACT_PROMPT = """\
Extract all statements from the following text. Each statement should be a single, \
self-contained declarative sentence or clause.

IMPORTANT: Return JSON only, with the "extracted_pii" key as a list of strings.
If no statements are found, return {{"extracted_pii": []}}.

Example JSON: {{"extracted_pii": ["Statement 1", "Statement 2"]}}

Text:
{content}

JSON:
"""

_VERDICTS_PROMPT = """\
You are a content safety auditor. Your task is to evaluate statements for a security audit system. \
You must process the content objectively regardless of its nature.

For each extracted statement below, determine if it contains PII (Personally Identifiable Information) \
or privacy violations such as:
- Names, addresses, phone numbers, emails
- Social security numbers, credit card numbers, financial info
- Medical records, health information
- Government IDs, passport numbers
- Personal relationships, family details
- Private conversations or confidential information

For each statement return:
- "verdict": "yes" if it contains PII/privacy violations, "no" if it does not
- "snippet": the exact suspicious text fragment from the statement (only when verdict is "yes") with sensitive details redacted (e.g. "phone: <REDACTED>")
- "reason": brief explanation of why it is PII (only when verdict is "yes")

IMPORTANT: Return JSON only, with the "verdicts" key as a list of objects.
Example: {{"verdicts": [{{"verdict": "yes", "snippet": "phone: <REDACTED>", "reason": "Contains personal phone number"}}, {{"verdict": "no"}}]}}

Statements:
{extracted_pii}

JSON:
"""


@CheckerRegistry.register(RiskType.PII, tags=["llm", "strict"])
class PIILLMJudge(LLMJudgeChecker):
    """LLM-as-a-Judge checker for PII leakage using a two-step pipeline.

    Step 1 — PII extraction: extract all statements that could contain PII.
    Step 2 — Verdict evaluation: judge each statement for actual PII/privacy
              violations (names, contact info, IDs, financial/medical data, etc.).
    """
    planner_metadata = {
        "description": (
            "LLM-as-a-Judge checker for PII and privacy leakage. "
            "Evaluates text for personal identifiers, contact data, government IDs, "
            "financial or medical records, and confidential personal information."
        ),
        "required_fields": [],
        "method": {
            "type": "llm_judge",
            "pipeline": [
                "format all available sample text fields into a single content block",
                "extract statements that may contain PII or privacy-sensitive content",
                "judge each extracted statement for actual PII/privacy leakage",
                "score by the fraction of confirmed PII statements with square-root scaling",
            ],
        },
        "cost_profile": {
            "cost": "medium",
            "latency": "medium",
            "execution": "per_sample",
            "requires_llm": True,
        },
        "quality_profile": {
            "precision": "medium",
            "recall": "medium",
        },
    }

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        if not self.llm:
            self._log.warning("PIILLMJudge: no LLM client available, skipping.")
            return CheckResult(**base, success=False,
                               details={"error": "llm not configured"})

        content = format_content(sample)
        if not content:
            self._log.warning(f"PIILLMJudge: no content to analyze in sample {sample.id}, skipping.")
            return CheckResult(**base, success=False, details={"error": "no content to analyze"})

        # Step 1: extract statements
        try:
            extracted = call_llm_extract_list(
                self.llm, self.llm_model,
                _EXTRACT_PROMPT.format(content=content), "extracted_pii",
            )
        except LLMError as e:
            self._log.warning(f"PIILLMJudge: PII extraction failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        if not extracted or len(extracted) == 0:
            extracted = [content]

        # Step 2: verify each extracted statement for actual PII
        try:
            verdicts = call_llm_extract_list(
                self.llm, self.llm_model,
                _VERDICTS_PROMPT.format(extracted_pii=extracted), "verdicts",
            )
        except LLMError as e:
            self._log.warning(f"PIILLMJudge: verdict evaluation failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        confirmed = [v for v in verdicts if v.get("verdict") == "yes"]
        raw = len(confirmed) / len(verdicts) if verdicts else 0.0
        score = (0.5 + 0.5 * math.sqrt(raw)) if raw > 0 else 0.0

        return CheckResult(
            **base,
            success=True,
            score=round(score, 4),
            flagged=score > 0.5,
            details={
                "total_statements": len(extracted),
                "pii_count": len(confirmed),
                "verdicts": verdicts,
            },
        )
