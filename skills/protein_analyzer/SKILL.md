---
name: protein_analyzer
description: Analyze protein sequences or identifiers for sequence properties, annotations, and scientific biology summaries.
allowed-tools:
  - python
  - network
---

## Usage Instructions

Use this skill when the user asks to analyze protein sequences, protein identifiers, molecular properties, or biology annotations.

## Input Expectations

- `data`: protein sequence, identifier, or list of protein records.
- Optional analysis mode or source parameters accepted by the internal protein backend.

## Output Expectations

Return protein analysis results with calculated properties, annotations, provenance, and any generated artifacts.

## Clarification Hints

Ask whether the input is a raw sequence or an identifier if ambiguous. Ask whether network enrichment is allowed when external annotation is needed.

## Examples

```json
{"id": "protein_analysis", "op": "invoke_skill", "skill": "protein_analyzer", "input": {"data": "$data"}, "output": "analysis"}
```
