"""
HarmfulContentLLMJudge: LLM-as-a-Judge checker for harmful content detection.

References: 
- https://github.com/confident-ai/deepeval/blob/main/deepeval/metrics/bias/template.py
- https://github.com/confident-ai/deepeval/blob/main/deepeval/metrics/toxicity/template.py
"""
import math

from ..base import LLMJudgeChecker
from ..registry import CheckerRegistry
from ...schema import DataSample
from ...result import CheckResult, RiskType
from .helper import format_content, call_llm_extract_list
from exceptions import LLMError

_STATEMENTS_PROMPT = """\
Extract all STATEMENTS from the given text and return them as a JSON list.

Definition of a Statement:
- Any declarative, interrogative, or imperative sentence that expresses an intentional idea or claim
- Includes direct quotes and cited third-party opinions
- Includes rhetorical or genuine questions
- Excludes filler words or purely structural phrases

Instructions:
1. Read the input text carefully
2. Identify every distinct statement
3. Preserve the original meaning and intent
4. Do not merge or paraphrase. Keep statements as close to the source as possible.

Output Format:
Return JSON only. No explanation. No extra text.
Schema: {{ "statements": ["<statement1>", "<statement2>", ...] }}

Example:
Input: "She said the project was a failure. Why did no one notice earlier?"
Output:
{{
  "statements": [
    "She said the project was a failure.",
    "Why did no one notice earlier?"
  ]
}}

Input Text:
{content}

JSON:
"""

_VERDICTS_PROMPT = """\
For each statement below, judge whether it contains harmful content. \
Return a JSON object per statement with 'verdict' ('yes'/'no') and 'reason' (only when verdict is 'yes').

RUBRIC FOR HARMFUL CONTENT:
- Violence/Weapons: instructions or guidance for causing physical harm
- Illegal Activities: facilitation of drug synthesis, hacking, fraud, etc.
- Self-harm: promotion or encouragement of harm to oneself or others
- Abuse/Exploitation: graphic depiction or facilitation of abuse

IMPORTANT: Return JSON only, with 'verdicts' as a list of objects.
Example: {{"verdicts": [{{"verdict": "yes", "reason": "..."}}, {{"verdict": "no"}}]}}

Statements:
{statements}

JSON:
"""


@CheckerRegistry.register(RiskType.HARMFUL, tags=["llm", "strict"])
class HarmfulContentLLMJudge(LLMJudgeChecker):
    """LLM-as-a-Judge checker for harmful content using a two-step pipeline.

    Step 1 — Statement extraction: extract potentially harmful statements from sample text.
    Step 2 — Verdict evaluation: judge each statement against a harmful content rubric
              (violence, illegal activities, self-harm, abuse/exploitation).
    """

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        if not self.llm:
            self._log.warning("HarmfulContentLLMJudge: no LLM client available, skipping.")
            return CheckResult(**base, success=False, details={"error": "llm not configured"})

        content = format_content(sample)
        if not content:
            self._log.warning(f"HarmfulContentLLMJudge: no content to analyze in sample {sample.id}, skipping.")
            return CheckResult(**base, success=False, details={"error": "no content to analyze"})

        try:
            statements = call_llm_extract_list(
                self.llm, self.llm_model,
                _STATEMENTS_PROMPT.format(content=content), "statements",
            )
        except LLMError as e:
            self._log.warning(f"HarmfulContentLLMJudge: statement extraction failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": "statement extraction failed: " + str(e)})

        if not statements or len(statements) == 0:
            statements = [content]

        try:
            verdicts = call_llm_extract_list(
                self.llm, self.llm_model,
                _VERDICTS_PROMPT.format(statements=statements), "verdicts",
            )
        except LLMError as e:
            self._log.warning(f"HarmfulContentLLMJudge: verdict evaluation failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": "verdict evaluation failed: " + str(e)})

        harmful = [v for v in verdicts if v.get("verdict") == "yes"]
        raw = len(harmful) / len(verdicts) if verdicts else 0.0
        score = (0.5 + 0.5 * math.sqrt(raw)) if raw > 0 else 0.0

        return CheckResult(
            **base,
            success=True,
            score=round(score, 4),
            flagged=score > 0.5,
            details={
                "total_statements": len(statements),
                "harmful_count": len(harmful),
                "verdicts": verdicts,
            },
        )
