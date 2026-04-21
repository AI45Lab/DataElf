"""
ToxicityLLMJudge: LLM-as-a-Judge checker for toxicity detection.

References: https://github.com/confident-ai/deepeval/blob/main/deepeval/metrics/toxicity/template.py
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
For each opinion below, judge whether it is toxic. Return a JSON object per opinion with \
'verdict' ('yes'/'no'), 'snippet' (exact toxic text fragment, only when verdict is 'yes'), \
and 'reason' (only when verdict is 'yes').

RUBRIC FOR TOXICITY:
- Personal Attacks: direct insults or attacks on a person's intelligence/character
- Mockery: sarcastic or ridiculing language intended to belittle
- Hate: extreme hostility or contempt toward a person or group
- Dismissive Statements: language that demeans others' contributions as worthless
- Threats or Intimidation: language implying harm or negative consequences

IMPORTANT: Return JSON only, with 'verdicts' as a list of objects.
Example: {{"verdicts": [{{"verdict": "yes", "snippet": "you are an idiot", "reason": "..."}}, {{"verdict": "no"}}]}}

Statements:
{statements}

JSON:
"""


@CheckerRegistry.register(RiskType.TOXICITY, tags=["llm", "strict"])
class ToxicityLLMJudge(LLMJudgeChecker):
    """LLM-as-a-Judge checker for toxicity detection using a two-step pipeline.

    Step 1 — Statement extraction:
        Ask the LLM to extract statements from the sample text (not facts).

    Step 2 — Verdict evaluation:
        For each extracted statement, ask the LLM whether it is toxic,
        using the rubric in ToxicityTemplate (personal attacks, mockery,
        hate, dismissive statements, threats/intimidation).
    """

    def check(self, sample: DataSample) -> CheckResult:
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        if not self.llm:
            self._log.warning("ToxicityLLMJudge: no LLM client available, skipping.")
            return CheckResult(**base, success=False, details={"error": "llm not configured"})

        content = format_content(sample)
        if not content:
            self._log.warning("ToxicityLLMJudge: no content to analyze, skipping.")
            return CheckResult(**base, success=False, details={"error": "no content to analyze"})

        # Step 1: extract statements from all text fields
        try:
            statements = call_llm_extract_list(
                self.llm, self.llm_model,
                _STATEMENTS_PROMPT.format(content=content), "statements",
            )
        except LLMError as e:
            self._log.warning(f"ToxicityLLMJudge: statement extraction failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": "statement extraction failed: " + str(e)})

        if not statements or len(statements) == 0:
            statements = [content]

        # Step 2: evaluate each statement for toxicity
        try:
            verdicts = call_llm_extract_list(
                self.llm, self.llm_model,
                _VERDICTS_PROMPT.format(statements=statements), "verdicts",
            )
        except LLMError as e:
            self._log.warning(f"ToxicityLLMJudge: verdict evaluation failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": "verdict evaluation failed: " + str(e)})

        toxic = [v for v in verdicts if v.get("verdict") == "yes"]
        raw = len(toxic) / len(verdicts) if verdicts else 0.0
        score = (0.5 + 0.5 * math.sqrt(raw)) if raw > 0 else 0.0

        return CheckResult(
            **base,
            success=True,
            score=round(score, 4),
            flagged=score > 0.5,
            details={
                "total_statements": len(statements),
                "toxic_count": len(toxic),
                "verdicts": verdicts,
            },
        )
