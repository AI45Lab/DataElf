---
name: data_scoring
description: Score instruction or text datasets for quality, value, complexity, perplexity, instruction-following difficulty, and education-style usefulness.
allowed-tools:
  - python
  - llm
---

## Usage Instructions

Use this skill when the user asks to evaluate, rank, score, or measure data quality or data value. It is commonly used before data selection.

## Input Expectations

- `data`: list of dataset records.
- `scorer`: scoring method. Use `dataelf` when the user does not specify a method.
- Optional scorer-specific parameters are accepted by the internal scoring backend.

## Output Expectations

Return scored records with score fields and scoring metadata that can be consumed by `data_select`.

## Clarification Hints

Ask for the scoring method only when the user requires a specific method but does not name one. Otherwise use the DataElf default scorer.

## Examples

```json
{"id": "score", "op": "invoke_skill", "skill": "data_scoring", "input": {"data": "$data", "scorer": "dataelf"}, "output": "scored"}
```
