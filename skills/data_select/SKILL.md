---
name: data_select
description: Select a budgeted subset from scored or raw datasets using quality-aware, diversity-aware, or cluster-aware selection strategies.
allowed-tools:
  - python
---

## Usage Instructions

Use this skill when the user asks to select, curate, downsample, or build a smaller training/evaluation subset from a dataset. If records have not been scored and the user asks for value-aware selection, plan a `data_scoring` step first.

## Input Expectations

- `data`: list of records, preferably already scored for value-aware selection.
- `dataset_name`: source dataset name for cache identity.
- `budget`: target number of samples.
- `strategy`: selection strategy such as proportional or top-k.
- `n_clusters`: optional cluster count for diversity-aware selection.
- `output_dir`: optional relative output directory.

## Output Expectations

Return selected records and selection metadata/artifacts.

## Clarification Hints

Ask for a selection budget if the user does not provide one. Ask whether to prioritize quality, diversity, or a balance when the request is ambiguous and the choice matters.

## Examples

```json
{"id": "select", "op": "invoke_skill", "skill": "data_select", "input": {"data": "$scored", "dataset_name": "alpaca_data", "budget": 500, "strategy": "proportional"}, "output": "selected"}
```
