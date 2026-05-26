---
name: security_audit
description: Audit datasets for AI safety and data security risks including PII, secrets, toxicity, harmful content, bias, prompt injection, jailbreak, and alignment-quality issues.
allowed-tools:
  - python
  - llm
---

## Usage Instructions

Use this skill when the user asks to inspect a dataset for security, privacy, safety, policy, jailbreak, prompt-injection, harmful-content, toxicity, bias, or related data-risk issues.

The DataElf orchestrator should load the dataset first, then invoke this skill with the loaded records. If the user names exact checkers, pass them as `checker_names`. If the user asks for the configured default audit, omit `checker_names` so the skill can use its policy defaults.

## Input Expectations

- `data`: list of dataset records.
- `checker_names`: optional list of checker class names such as `PIIRule`, `SecretRule`, `ToxicityKeywordRule`, `HarmfulContentLLMJudge`, `ToxicityLLMJudge`, `PIILLMJudge`, `BiasLLMJudge`, `PromptInjectionLLMJudge`, or `JailbreakLLMJudge`.
- `max_workers`: optional integer for checker parallelism.

## Output Expectations

Return a structured audit result with sample-level risk findings, aggregate risk distribution, security score, metadata, and generated artifacts such as report paths when available.

## Clarification Hints

Ask for a dataset name if none is provided and multiple datasets are available. Ask for checker names only when the user explicitly requests a custom checker set but does not provide exact names. Offer cheap, balanced, and stronger checker recommendations when the user asks for options.

## Examples

```json
{
  "version": "dataelf_execution_plan_v1",
  "steps": [
    {"id": "load_data", "op": "load_dataset", "dataset": "security_audit_samples", "output": "data"},
    {"id": "audit", "op": "invoke_skill", "skill": "security_audit", "input": {"data": "$data", "checker_names": ["PIIRule", "SecretRule"]}, "output": "audit_result"},
    {"id": "save", "op": "save_result", "input": "$audit_result"}
  ]
}
```
