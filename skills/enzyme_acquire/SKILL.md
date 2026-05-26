---
name: enzyme_acquire
description: Acquire enzyme information from names, EC numbers, or UniProt identifiers, returning normalized scientific records and provenance.
allowed-tools:
  - python
  - network
---

## Usage Instructions

Use this skill for enzyme lookup, enrichment, or acquisition tasks involving enzyme names, EC numbers, UniProt IDs, or mixed enzyme identifiers.

## Input Expectations

- `data`: string or list of enzyme identifiers.
- Optional source and output controls accepted by the internal scientific backend.

## Output Expectations

Return normalized enzyme records with identifiers, names, functions, sequence or database metadata when available, and provenance.

## Clarification Hints

Ask whether network enrichment is allowed when deployment policy is offline or unclear and the request requires external sources.

## Examples

```json
{"id": "enzyme_lookup", "op": "invoke_skill", "skill": "enzyme_acquire", "input": {"data": ["hexokinase"]}, "output": "enzymes"}
```
