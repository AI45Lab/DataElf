# Data Scoring Tool

## Overview

The `data_scoring` tool assigns quality scores to each sample in a dataset, serving as a prerequisite step for downstream data selection. It supports pluggable scoring methods: users can switch scorers via configuration files, and can run multiple scorers on the same dataset to obtain multi-dimensional quality signals.

The platform currently integrates 9 scorers covering different quality assessment dimensions:

| Method | Description |
|--------|-------------|
| `dataelf` | **Default and recommended.** Converts `ifd` and `deita_q` scores to percentile ranks and fuses them with equal weight, producing a single quality score that reflects question-answer clarity, accuracy, and reasoning difficulty |
| `ppl` | Measures data predictability from a probability perspective: the more natural and fluent the model finds the text, the higher the quality score; lower-scoring samples are those the model finds unnatural |
| `norm_loss` | Similar in spirit to `ppl`, measures whether text is natural and fluent from an information compression perspective |
| `ifd` | Compares the relative difficulty of generating the same response with and without the instruction; the more the instruction helps, the higher the quality |
| `deita_q` | Evaluates whether instructions and responses are clear and accurate |
| `deita_c` | Evaluates instruction complexity |
| `deberta` | Quality classification via a purpose-built classifier trained on human-annotated data, assessing text coherence, grammatical accuracy, etc. |
| `fineweb_edu` | Focuses on the educational value of training samples, e.g. whether they contain clear explanations and structured information |
| `ask_llm` | Evaluates quality by directly asking a large language model whether a sample constitutes high-quality data |

All scorers output scores normalized to 0-5 (higher = better). Records with an empty `output` field are marked as invalid (-1) and excluded from downstream selection.

The figure below shows the overall performance (normalized mean of AlpacaEval 2.0, MT-Bench, and GSM8K) after fine-tuning `Qwen2.5-7B` on 9,000 samples selected by each scorer from the Alpaca-52k dataset. `DataElf` surpasses the full-data baseline using less than 1/5 of the data, significantly outperforming all other scorers.

![Scorer comparison experiment](data_scoring_benchmark.png)

Use cases:

- Quality assessment and cleaning of large-scale training data
- Effectiveness analysis and comparison of different scorers


## Input Schema

The tool currently accepts data in Alpaca format, where each record is a `dict`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `instruction` | `str` | Yes | Instruction text |
| `input` | `str` | No | Supplementary input (may be empty string) |
| `output` | `str` | Yes | Response text (empty marks as invalid) |

Example input sample:
```json
{
    "instruction": "Give three tips for staying healthy.",
    "input": "",
    "output": "1. Eat a balanced diet. 2. Exercise regularly. 3. Get enough sleep."
}
```


## Parameters

Called via `run_tool("data_scoring", ...)`. The following parameters can be passed:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `data` | `list[dict]` | Yes | — | Dataset name to score |
| `scorer` | `str` | No | `dataelf` | Scoring method: `dataelf`, `ask_llm`, `ppl`, `ifd`, `norm_loss`, `deita_q`, `deita_c`, `deberta`, `fineweb_edu` |
| `model` | `str` | Configurable in `tools/scoring/defaults.yaml` | — | Scoring model path |
| `batch_size` | `int` | Configurable in `tools/scoring/defaults.yaml` | — | Batch size |
| `output_dir` | `str` | No | `outputs/scores/<scorer>/` | Directory to save scoring results |


## Output

`run_tool("data_scoring", ...)` returns a list of scored data records (`list[dict]`), with a `score` field added to each original record:

```yaml
- instruction: str
  input: str
  output: str
  score: float                   # Quality score (0-5), -1 if invalid
```


## Algorithms

### DataElf Hybrid Scoring (Default)

`dataelf` is the platform's default and recommended scorer. It internally runs `ifd` and `deita_q` as sub-scorers, converts their scores to percentile ranks, and fuses them:

```
fused = alpha * rank(ifd) + (1 - alpha) * rank(deita_q)
```

- `alpha=0.5` by default, giving equal weight to both signals
- `ifd` captures reasoning capability; `deita_q` captures conversational clarity and accuracy
- Output is scaled to [0, 5] to match other scorers for easy comparison

### Score Caching

All scorer results are automatically cached to `outputs/scores/<scorer>/scored_data.json`. Subsequent runs on the same dataset reuse the cache, avoiding redundant computation.

`dataelf` additionally caches sub-scorer results to `outputs/scores/ifd/` and `outputs/scores/deita_q/`, enabling bidirectional reuse:
- Running `dataelf` → caches `ifd` and `deita_q` → future standalone `ifd` or `deita_q` runs hit the cache
- Running `ifd` and `deita_q` separately first → future `dataelf` run reuses both caches


## Example

### Pipeline DSL Example 1: Using the default scorer
```python
log_step("Loading dataset")

data = load_dataset("alpaca_data")

log_step("Scoring data quality")

scored = run_tool(
    "data_scoring",
    data=data
)

log_step(f"Scored {len(scored)} records")

save_result(scored)
```

### Pipeline DSL Example 2: Specifying a scoring method
```python
log_step("Loading dataset")

data = load_dataset("alpaca_data")

log_step("Scoring data quality with IFD")

scored = run_tool(
    "data_scoring",
    data=data,
    scorer="ifd"
)

log_step(f"Scored {len(scored)} records")

save_result(scored)
```

### CLI Examples
```bash
# Score with the default scorer (dataelf)
elf run "score the alpaca dataset using dataelf" -c config.yaml -v

# Score with a specific method
elf run "score the alpaca data with ifd" -c config.yaml -v
```


## Configuration

Default parameters are configured in `tools/scoring/defaults.yaml`, for example:

```yaml
dataelf:
  alpha: 0.5
  ifd_model: <model-path>
  ifd_batch_size: 256
  deita_q_model: <model-path>
  deita_q_batch_size: 256
  max_length: 512
```

## Dependencies

| Dependency | Purpose | Required |
|------------|---------|----------|
| `torch` + `transformers` | Model inference for all scorers | Yes |
| GPU | Model loading | Yes |
