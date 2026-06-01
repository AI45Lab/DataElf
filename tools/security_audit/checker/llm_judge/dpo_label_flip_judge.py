"""
DPOLabelFlipLLMJudge — LLM-as-a-Judge checker for DPO label flipping.

Reference: 
- https://arxiv.org/abs/2501.14506
- https://github.com/MigoXLab/dingo/blob/23b3a00e/dingo/model/llm/text_quality/llm_text_quality_v5.py
"""

import json

from ..base import LLMJudgeChecker
from ..registry import CheckerRegistry
from ...schema import DataSample, DataFormat
from ...result import CheckResult, RiskType
from exceptions import LLMConnectionError, LLMResponseError, LLMError



_QUALITY_SCORE_PROMPT = """\
# Role
You are an expert in assessing pretraining data quality for large language models.

# Goal
Evaluate whether this text is suitable for LLM pretraining. Focus on issues that would negatively impact model learning, not minor imperfections.

# Quality Dimensions

## 1. Completeness
**Impact**: Broken structures prevent models from learning correct formatting patterns.

**Check for**:
- **Error_Formula**: Mathematical expressions with **unmatched delimiters** or **unclosed environments**

  ⚠️ **Normal patterns (DO NOT flag)**:
  - Mixing inline ($...$) and display ($$...$$) formulas
  - Using \\begin{{align}}...\\end{{align}} within $$...$$
  - Line breaks with \\\\ in alignment environments
  - HTML tags: <sub>x</sub>, <sup>2</sup> for subscripts/superscripts
  - Mixing LaTeX and HTML in web-extracted content

  ✅ **Only flag when**:
  - Delimiters unmatched: $ without closing $ (LaTeX context, not dollar signs)
  - Environments unclosed: \\begin{{align}} without \\end{{align}}
  - Syntax broken: \\frac{{a}}{{b missing closing }}
  - HTML tags unclosed: <sub>text without </sub>

  ⚠️ **Important**: Distinguish LaTeX $ from dollar signs ($100)
  - Dollar sign: "$100", "$5.99" (followed by numbers) → NOT LaTeX
  - LaTeX delimiter: "$x$", "$\\alpha$" (contains math symbols) → IS LaTeX
  - Example: "The price is $100 and equation $x=y$ costs $50" has 4 dollar symbols but only 2 are LaTeX delimiters (and they match)

  - Example (BAD): "$x^2 + y^2 is broken here $$a = b$$$"
    (First LaTeX $ never closes, extra $ at end)
  - Example (GOOD): "The item costs $100 and satisfies $x^2 + y^2 = z^2$ where price is $50"
    (Dollar signs for money + proper LaTeX pair)
  - Impact: Only flag errors that prevent >50% of mainstream parsers (pdflatex, MathJax, KaTeX, Pandoc, Jupyter) from rendering

- **Error_Table**: Table structures that are malformed or unreadable
  - Example (BAD): Misaligned columns, missing headers, or garbled HTML tags
  - Impact: Models cannot learn proper table representation

- **Error_Code**: Code blocks with formatting corruption
  - Example (BAD): Line numbers mixed with code, broken syntax highlighting markers
  - Impact: Teaches incorrect code structure

**Key Question**: "Can the model learn proper formatting from this structure?"

---

## 2. Effectiveness
**Impact**: Noise prevents models from learning meaningful semantic patterns.

**Check for**:
- **Error_Garbled_Characters**: Encoding issues or anti-crawler artifacts
  - Example (BAD): "â€™" (broken UTF-8), "□□□" (placeholder chars), "ï»¿" (BOM)
  - Threshold: >1% of characters are garbled
  - Impact: Corrupts token distributions

- **Error_Words_Stuck**: Missing spaces break tokenization
  - Example (BAD): "Thequickbrownfoxjumpsoverthelazydog"
  - Threshold: >1% of text has word boundaries missing
  - Impact: Wrong subword tokenization patterns

- **Error_Lack_Punctuation**: Sentence boundaries unclear
  - Example (BAD): "I like apples they are red also I like oranges"
  - Impact: Models cannot learn sentence segmentation

**Key Question**: "Would a human find this readable and coherent?"

---

## 3. Similarity
**Impact**: Repetitive content reduces training efficiency and causes memorization.

**Check for**:
- **Error_Duplicate**: Excessive repetition that dominates the text
  - Example (BAD): "I like blue. I like blue. I like blue. I like blue..." (>30% duplicate)
  - Threshold: Same sentence/phrase repeats >5 times OR duplicate ratio >30%
  - Impact: Over-represents certain patterns

**Key Question**: "Does this text provide diverse training signal?"

---

## 4. Security
**Impact**: Harmful content should not be learned by models.

**Check for**:
- **Error_Politics**: Content promoting extremism, terrorism, ethnic hatred
- **Error_Prohibition**: Violence, pornography, gambling, drugs

**Key Question**: "Is this content safe for model training?"

---

# Evaluation Principles

1. **Focus on Training Impact**: Only flag issues that significantly harm LLM learning
2. **Severity Matters**: Minor typos are OK; systemic corruption is not
3. **Context Awareness**: Academic formulas are expected in papers; garbled text never is
4. **Threshold-Based**: Use quantitative checks (>1%, >30%, >5 times) when possible

---

# Workflow

1. **Quick Scan**: Does the text look generally readable and well-formed?
2. **Identify Category**: If problematic, which dimension is most severely affected?
3. **Verify Impact**: Would this issue meaningfully harm model training?
4. **Assign Label**:
   - Score: 1 (suitable for training) or 0 (unsuitable)
   - Type: 'Good' OR one of ['Completeness', 'Effectiveness', 'Similarity', 'Security']
   - Name: Specific error type (see above)
   - Reason: Brief explanation (1-2 sentences)

# Scoring
- score: 1 (acceptable quality) or 0 (has a significant quality problem)
- type: "Good" if score=1; otherwise one of ["Completeness", "Accuracy", "Relevance", "Safety"]
- name: Specific flaw label (e.g. "Error_Truncated", "Error_Hallucination", "Error_OffTopic", "Error_Harmful") or "None"
- reason: One concise sentence

# Output
Return JSON only: {{"score": 0/1, "type": "", "name": "", "reason": ""}}

# Input

Instruction:
{instruction}

Response:
{response}

JSON:
"""


_PAIRWISE_COMPARE_PROMPT = """\
# Role
You are an expert evaluator for LLM preference alignment data.

# Goal
Given an instruction and two responses (A and B), decide which response a human
rater would PREFER as the higher-quality answer.

Evaluation criteria (in priority order):
1. **Helpfulness** — More fully and correctly solves the user's request
2. **Accuracy** — Fewer factual errors or logical gaps
3. **Clarity** — Better structured, easier to understand
4. **Conciseness** — No unnecessary verbosity, but not at the expense of completeness
5. **Safety** — Avoids harmful or misleading content

# Rules
- Do NOT prefer a response simply because it is longer.
- Do NOT prefer a response because it is more polite or sycophantic if it sacrifices correctness.
- If both responses are nearly identical in quality, output "tie".

# Output
Return JSON only: {{"winner": "A" | "B" | "tie", "reason": ""}}

# Input

Instruction:
{instruction}

Response A (chosen):
{chosen}

Response B (rejected):
{rejected}

JSON:
"""

# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

@CheckerRegistry.register(RiskType.LABEL_FLIPPING, tags=["llm", "dpo"])
class DPOLabelFlipLLMJudge(LLMJudgeChecker):
    """LLM-as-a-Judge checker for DPO label flipping.

    Step 1 — Score each response independently on quality dimensions (0/1).
    Step 2 — When quality scores are tied, run a direct pairwise comparison.
    """
    planner_metadata = {
        "description": (
            "LLM-as-a-Judge checker for DPO label flipping. "
            "Detects cases where the rejected response appears preferable to the chosen response."
        ),
        "required_fields": ["chosen_response", "rejected_response"],
        "method": {
            "type": "llm_judge",
            "pipeline": [
                "skip non-DPO samples",
                "extract the instruction plus chosen and rejected responses",
                "score each response independently for training quality",
                "run pairwise comparison when independent scores are tied",
                "flag hard flips and pairwise flips where rejected is preferred",
            ],
        },
        "cost_profile": {
            "cost": "high",
            "latency": "high",
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

        if sample.dataset_type != DataFormat.DPO:
            return CheckResult(**base, success=True, details={"skipped": f"not a DPO sample (got {sample.dataset_type!r}), no label flipping risk"})
        
        chosen = sample.chosen_response.content if sample.chosen_response else ""
        rejected = sample.rejected_response.content if sample.rejected_response else ""

        if not chosen or not rejected:
            self._log.warning(f"DPOLabelFlipLLMJudge: missing chosen or rejected response in sample {sample.id}, skipping.")
            return CheckResult(**base, success=False,
                               details={"error": "missing chosen_response or rejected_response"})

        instruction = self._extract_instruction(sample)

        if not self.llm:
            self._log.warning("DPOLabelFlipLLMJudge: no LLM client available, skipping.")
            return CheckResult(**base, success=False, details={"error": "llm not configured"})

        # Step 1: independent quality scores
        try:
            chosen_eval = self._score_response(instruction, chosen)
            rejected_eval = self._score_response(instruction, rejected)
        except LLMError as e:
            self._log.warning(f"DPOLabelFlipLLMJudge: quality scoring failed on sample {sample.id}: {e}")
            return CheckResult(**base, success=False, details={"error": str(e)})

        chosen_score = chosen_eval.get("score", 1)
        rejected_score = rejected_eval.get("score", 1)

        details: dict = {
            "instruction_preview": instruction[:200] if instruction else "",
            "chosen_eval": chosen_eval,
            "rejected_eval": rejected_eval,
        }

        # Hard flip: chosen is bad, rejected is good → definite label error
        if chosen_score == 0 and rejected_score == 1:
            details["verdict"] = "hard_flip"
            return CheckResult(**base, success=True, score=1.0, flagged=True, details=details)

        # Correct labeling: chosen is good, rejected is bad → no flip
        if chosen_score == 1 and rejected_score == 0:
            details["verdict"] = "correct"
            return CheckResult(**base, success=True, score=0.0, flagged=False, details=details)

        # Step 2: pairwise comparison (both scored the same)
        try:
            pairwise = self._pairwise_compare(instruction, chosen, rejected)
        except LLMError as e:
            self._log.warning(f"DPOLabelFlipLLMJudge: pairwise comparison failed on sample {sample.id}: {e}")
            # Conservative: treat tied quality as borderline rather than surfacing an error
            details["verdict"] = "uncertain"
            return CheckResult(**base, success=True, score=0.4, flagged=False, details=details)
        
        details["pairwise"] = pairwise
        winner = pairwise.get("winner", "tie")

        if winner == "B":  # rejected is preferred → label flip
            details["verdict"] = "pairwise_flip"
            score = 0.75
        elif winner == "A":  # chosen is preferred → correct
            details["verdict"] = "pairwise_correct"
            score = 0.0
        else:  # tie
            details["verdict"] = "tie"
            score = 0.4

        return CheckResult(
            **base,
            success=True,
            score=round(score, 4),
            flagged=score >= 0.5,
            details=details,
        )


    def _extract_instruction(self, sample: DataSample) -> str:
        """Extract the last user instruction from sample messages."""
        for m in reversed(sample.messages):
            if m.role == "user":
                parts = m.get_text_parts()
                if parts:
                    return "\n".join(parts)
        return ""

    def _score_response(self, instruction: str, response: str) -> dict:
        """Call LLM to score a single response.
        """
        prompt = _QUALITY_SCORE_PROMPT.format(instruction=instruction, response=response)
        try:
            raw = self.llm.generate(model=self.llm_model, prompt=prompt)
        except Exception as e:
            raise LLMConnectionError(f"_score_response LLM call failed: {e}") from e
        try:
            data, _ = json.JSONDecoder().raw_decode(raw, raw.index("{"))
            data["score"] = int(bool(data.get("score", 1)))
            return data
        except Exception as e:
            raise LLMResponseError(f"_score_response parse failed: {e} | raw response: {raw!r}") from e

    def _pairwise_compare(self, instruction: str, chosen: str, rejected: str) -> dict:
        """Call LLM for pairwise comparison.
        """
        prompt = _PAIRWISE_COMPARE_PROMPT.format(
            instruction=instruction, chosen=chosen, rejected=rejected
        )
        try:
            raw = self.llm.generate(model=self.llm_model, prompt=prompt)
        except Exception as e:
            raise LLMConnectionError(f"_pairwise_compare LLM call failed: {e}") from e
        try:
            data, _ = json.JSONDecoder().raw_decode(raw, raw.index("{"))
            winner = data.get("winner", "tie").strip().upper()
            data["winner"] = "tie" if winner not in ("A", "B") else winner
            return data
        except Exception as e:
            raise LLMResponseError(f"_pairwise_compare parse failed: {e} | raw response: {raw!r}") from e
