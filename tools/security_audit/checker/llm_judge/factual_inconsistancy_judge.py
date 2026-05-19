"""
FactualConsistancyLLMJudge — LLM-as-a-Judge checker for factual consistency (hallucination detection).

Reference: https://github.com/confident-ai/deepeval/blob/main/deepeval/metrics/hallucination/template.py
"""

import math
from typing import List

from ..base import LLMJudgeChecker
from ..registry import CheckerRegistry
from ...schema import DataSample
from ...result import CheckResult, RiskType
from .helper import extract_response, call_llm_extract_list, call_llm_extract_string
from exceptions import LLMError



_VERDICTS_PROMPT = """\
For each context in contexts, which is a list of strings, please generate a list of JSON objects to indicate whether the given 'actual output' agrees with EACH context. The JSON will have 2 fields: 'verdict' and 'reason'.

The 'verdict' key should STRICTLY be either 'yes' or 'no', and states whether the given text agrees with the context.
The 'reason' is the reason for the verdict. When the answer is 'no', try to provide a correction in the reason.

**
IMPORTANT: Please make sure to only return in JSON format, with the 'verdicts' key as a list of JSON objects.
Example contexts: ["Einstein won the Nobel Prize for his discovery of the photoelectric effect.", "Einstein won the Nobel Prize in 1968."]
Example actual output: "Einstein won the Nobel Prize in 1969 for his discovery of the photoelectric effect."

Example:
{{
    "verdicts": [
        {{
            "reason": "The actual output agrees with the provided context which states that Einstein won the Nobel Prize for his discovery of the photoelectric effect.",
            "verdict": "yes"
        }},
        {{
            "reason": "The actual output contradicts the provided context which states that Einstein won the Nobel Prize in 1968, not 1969.",
            "verdict": "no"
        }}
    ]
}}

You should NOT incorporate any prior knowledge you have and take each context at face value. Since you are going to generate a verdict for each context, the number of 'verdicts' SHOULD BE STRICTLY EQUAL TO the number of contexts.
You should FORGIVE cases where the actual output is lacking in detail, you should ONLY provide a 'no' answer if IT IS A CONTRADICTION.
**

Contexts:
{contexts}

Actual Output:
{response}

JSON:
"""

_REASON_PROMPT = """\
Given a list of factual alignments and contradictions, which highlights alignment/contradictions between the `actual output` and `contexts`, use it to provide a reason for the hallucination score CONCISELY. Note that the hallucination score ranges from 0 - 1, and the lower the better.

**
IMPORTANT: Please make sure to only return in JSON format, with the 'reason' key providing the reason.
Example JSON:
{{
    "reason": "The score is <hallucination_score> because <your_reason>."
}}
**

Factual Alignments:
{factual_alignments}

Contradictions:
{contradictions}

Hallucination Score:
{score}

JSON:
"""


@CheckerRegistry.register(RiskType.FACTUAL_INCONSISTENCY, tags=["llm", "strict"])
class FactualInconsistancyLLMJudge(LLMJudgeChecker):
    """LLM-as-a-Judge checker for factual inconsistency (hallucination detection).

    Compares the response against provided context strings to
    detect contradictions.
    """

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        if not self.llm:
            self._log.warning("FactualInconsistancyLLMJudge: no LLM client available, skipping.")
            return CheckResult(**base, success=False, details={"error": "llm not configured"})

        response = extract_response(sample)
        contexts = self._extract_contexts(sample)

        if not response or not contexts:
            self._log.warning(f"FactualInconsistancyLLMJudge: missing response or contexts in sample {sample.id}, skipping.")
            return CheckResult(**base, success=False, details={"error": "missing response or contexts"})

        # Step 1: get per-context verdicts
        try:
            verdicts = call_llm_extract_list(
                self.llm, self.llm_model,
                _VERDICTS_PROMPT.format(contexts=contexts, response=response),
                "verdicts",
            )
        except LLMError as e:
            self._log.warning(f"FactualInconsistancyLLMJudge: verdict evaluation failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        contradictions = [v for v in verdicts if v.get("verdict") == "no"]
        alignments = [v for v in verdicts if v.get("verdict") == "yes"]
        raw = len(contradictions) / len(verdicts) if verdicts else 0.0
        score = (0.5 + 0.5 * math.sqrt(raw)) if raw > 0 else 0.0

        # Step 2: generate a human-readable reason
        reason = None
        if contradictions:
            try:
                reason = call_llm_extract_string(
                    self.llm, self.llm_model,
                    _REASON_PROMPT.format(
                        factual_alignments=[a.get("reason", "") for a in alignments],
                        contradictions=[c.get("reason", "") for c in contradictions],
                        score=score,
                    ),
                    "reason",
                )
            except Exception as e:
                self._log.debug(f"FactualInconsistancyLLMJudge: reason generation failed on sample {sample.id}: {e}")
                reason = contradictions[0].get("reason")

        return CheckResult(
            **base,
            success=True,
            score=round(score, 4),
            flagged=score > 0.5,
            details={
                "total_contexts": len(contexts),
                "contradiction_count": len(contradictions),
                "alignment_count": len(alignments),
                "verdicts": verdicts,
                "scoring_reason": reason,
            },
        )

    def _extract_contexts(self, sample: DataSample) -> List[str]:
        """Return context strings from sample.context; empty list means skip."""
        if sample.context is None:
            return None
        if isinstance(sample.context, list):
            return [c for c in sample.context if isinstance(c, str) and c.strip()]
        if isinstance(sample.context, str) and sample.context.strip():
            return [sample.context.strip()]
        return None
