---
name: skillrl_skill_extraction
description: Extract reusable skills from trajectory-style interaction data for agent behavior analysis and downstream skill injection.
allowed-tools:
  - python
  - llm
---

## Usage Instructions

Use this skill when the user asks to extract, distill, summarize, or evaluate skills from trajectories such as ALFWorld, WebShop, RiOSWorld, or similar agent traces.

## Input Expectations

- `data`: trajectory records or task traces.
- Optional extraction parameters accepted by the internal skill extraction backend.

## Output Expectations

Return extracted skills, supporting evidence, safety or utility metadata, and any benchmark artifacts.

## Clarification Hints

Ask for the trajectory source or dataset name if it is missing. Ask for the desired skill granularity when the user needs a specific abstraction level.

## Examples

```json
{"id": "extract_skills", "op": "invoke_skill", "skill": "skillrl_skill_extraction", "input": {"data": "$trajectories"}, "output": "skills"}
```
