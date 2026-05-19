"""SelfContradictionLLMJudge — LLM-as-a-Judge checker for self-contradiction detection in model responses.

Measures whether the model's actual output contains internal contradictions (i.e., the response contradicts itself). A higher score means more self-contradictions were found.
"""

import math

from ..base import LLMJudgeChecker
from ..registry import CheckerRegistry
from ...schema import DataSample
from ...result import CheckResult, RiskType
from .helper import extract_response, call_llm_extract_list, call_llm_extract_string
from exceptions import LLMError

_CLAIMS_PROMPT = """\
Please extract all factual claims or statements from the following text. Each claim should be a single, self-contained assertion.

IMPORTANT: Please make sure to only return in JSON format, with the 'claims' key as a list of strings.
Example text: "The capital of France is Paris. France is located in Europe. The capital of France is Lyon."

Example:
{{
    "claims": [
        "The capital of France is Paris.",
        "France is located in Europe.",
        "The capital of France is Lyon."
    ]
}}

Only extract explicit claims from the text. Do NOT add any claims that are not present in the text.
**

Text:
{response}

JSON:
"""

_CONTRADICTIONS_PROMPT = """\
Given the following list of claims extracted from a SINGLE response, determine whether any claims contradict each other. For each pair of contradictory claims, generate a JSON object with 'claim_a', 'claim_b', 'verdict', and 'reason'.

The 'verdict' key should STRICTLY be either 'yes' (they contradict each other) or 'no' (they do not contradict each other).
The 'reason' explains why the two claims contradict each other.

**
IMPORTANT: Please make sure to only return in JSON format, with the 'contradictions' key as a list of JSON objects.
Example claims: ["The capital of France is Paris.", "France is located in Europe.", "The capital of France is Lyon."]

Example:
{{
    "contradictions": [
        {{
            "claim_a": "The capital of France is Paris.",
            "claim_b": "The capital of France is Lyon.",
            "verdict": "yes",
            "reason": "These two claims contradict each other because they assert different cities as the capital of France."
        }}
    ]
}}

Only report pairs that are genuinely contradictory. Do NOT flag claims that are merely different or unrelated. A contradiction means two claims CANNOT both be true at the same time.
If there are no contradictions, return an empty list.
**

Claims:
{claims}

JSON:
"""

_REASON_PROMPT = """\
Given a list of self-contradictions found within a single response, provide a concise reason for the self-contradiction score. The score ranges from 0 - 1, where 0 means no self-contradictions and 1 means every claim is contradicted.

**
IMPORTANT: Please make sure to only return in JSON format, with the 'reason' key providing the reason.
Example JSON:
{{
    "reason": "The score is <score> because <your_reason>."
}}
**

Contradictions:
{contradictions}

Self-Contradiction Score:
{score}

JSON:
"""


@CheckerRegistry.register(RiskType.SELF_CONTRADICTION, tags=["llm", "strict"])
class SelfContradictionLLMJudge(LLMJudgeChecker):
    """LLM-as-a-Judge checker for self-contradiction detection in responses.

    Analyzes the assistant's response to find internal contradictions
    where the response says one thing and then contradicts itself later.

    Score = number_of_contradictions / total_claims  (0 = fully consistent).
    """

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        if not self.llm:
            self._log.warning("SelfContradictionLLMJudge: no LLM client available, skipping.")
            return CheckResult(**base, success=False, details={"error": "llm not configured"})

        response = extract_response(sample)

        if not response:
            self._log.warning(f"SelfContradictionLLMJudge: missing actual output in sample {sample.id}, skipping.")
            return CheckResult(**base, success=False, details={"error": "missing response"})

        # Step 1: extract claims from the response
        try:
            claims = call_llm_extract_list(
                self.llm, self.llm_model,
                _CLAIMS_PROMPT.format(response=response),
                "claims",
            )
        except LLMError as e:
            self._log.warning(f"SelfContradictionLLMJudge: claim extraction failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": "claim extraction failed: " + str(e)})

        if not claims or len(claims) < 2:
            return CheckResult(**base, success=False,
                               details={"skipped": "not enough claims to compare",
                                        "claim_count": len(claims) if claims else 0})

        # Step 2: detect contradictions among the claims
        try:
            contradictions = call_llm_extract_list(
                self.llm, self.llm_model,
                _CONTRADICTIONS_PROMPT.format(claims=claims),
                "contradictions",
            )
        except LLMError as e:
            self._log.warning(f"SelfContradictionLLMJudge: contradiction detection failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": "contradiction detection failed: " + str(e)})

        real_contradictions = [c for c in contradictions if c.get("verdict") == "yes"]
        raw = min(len(real_contradictions) / len(claims), 1.0) if claims else 0.0
        score = (0.5 + 0.5 * math.sqrt(raw)) if raw > 0 else 0.0 # mapping to (0.5, 1.0] range for better score distribution

        # Step 3: generate a human-readable reason
        reason = None
        if real_contradictions:
            try:
                reason = call_llm_extract_string(
                    self.llm, self.llm_model,
                    _REASON_PROMPT.format(
                        contradictions=[c.get("reason", "") for c in real_contradictions],
                        score=score,
                    ),
                    "reason",
                )
            except Exception as e:
                self._log.debug(f"SelfContradictionLLMJudge: reason generation failed on sample {sample.id}: {e}")
                reason = real_contradictions[0].get("reason")

        return CheckResult(
            **base,
            success=True,
            score=round(score, 4),
            flagged=score > 0.5,
            details={
                "total_claims": len(claims),
                "claims": claims,
                "contradiction_count": len(real_contradictions),
                "contradictions": real_contradictions,
                "scoring_reason": reason,
            },
        )
