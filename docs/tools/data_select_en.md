# Data Select Tool

## Overview

The `data_select` tool selects a specified number of high-quality samples from scored candidate data, typically used as a downstream step after `data_scoring`.

Simply sorting by score and taking the top K records tends to produce highly homogeneous selections lacking diversity in topic and difficulty. Instead, this tool first extracts semantic embeddings for each sample using an embedding model (`Llama-3.1-8B-Instruct`), then performs K-means clustering on these embeddings and allocates the total selection budget proportionally across clusters, and finally selects the top-scored records within each cluster. This strategy preserves diversity in the selected data while ensuring high quality within each cluster.

Use cases:

- Budget-constrained selection of high-quality training data with diversity guarantees


## Input Schema

The tool currently accepts scored data records in Alpaca format, where each record must include a `score` field:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `instruction` | `str` | Yes | Instruction text |
| `input` | `str` | No | Supplementary input |
| `output` | `str` | Yes | Response text |
| `score` | `float` | Yes | Quality score |

Example input sample:
```json
{
    "instruction": "Give three tips for staying healthy.",
    "input": "",
    "output": "1. Eat a balanced diet. 2. Exercise regularly. 3. Get enough sleep.",
    "score": 3.72
}
```


## Parameters

Called via `run_tool("data_select", ...)`. The following parameters can be passed:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `data` | `list[dict]` | Yes | — | List of scored data records (must contain `score` field) |
| `dataset_name` | `str` | Yes | — | Dataset name, used to organize embedding cache; should match the name used in `load_dataset()` |
| `budget` | `int` | Yes | — | Target number of samples to select |
| `strategy` | `str` | No | `proportional` | Quota allocation: `proportional` (by cluster size) or `uniform` (equal across clusters) |
| `output_dir` | `str` | Yes | — | Directory to save results, e.g. `outputs/<dataset_name>_data_<budget>` |


## Output

`run_tool("data_select", ...)` returns a list of selected data records (`list[dict]`), preserving original fields and scores:

```yaml
- instruction: str
  input: str
  output: str
  score: float
```

## Algorithms

### Diversity-Aware Selection Pipeline

1. **Filter invalid records**: Exclude samples with score < 0
2. **Load or extract embeddings**: Extract semantic vectors for each sample using an embedding model (default: `Llama-3.1-8B-Instruct`), with automatic caching
3. **K-means clustering**: Cluster embedding vectors to partition data into semantic subgroups
4. **Quota allocation**: Distribute the total selection budget across clusters by strategy
   - `proportional`: Allocate proportionally by cluster size; larger clusters get more samples (default and recommended)
   - `uniform`: Allocate equally across clusters
5. **Within-cluster TopK selection**: Select top-scored records within each cluster

### Embedding Caching

Embeddings are cached at `outputs/embeddings/<dataset_name>/embeddings_<N>.npy`, where `<dataset_name>` matches the dataset name used in `load_dataset()` and `<N>` is the total record count. Subsequent runs on the same dataset reuse cached embeddings without re-extraction.

**Important:** Always pass `dataset_name` in the pipeline to ensure consistent cache hits.


## Example

### Pipeline DSL Example: Score then select
```python
log_step("Loading dataset")

data = load_dataset("alpaca_data")

log_step("Scoring data quality")

scored = run_tool(
    "data_scoring",
    data=data,
    scorer="dataelf"
)

log_step(f"Scored {len(scored)} records")

log_step("Running diversity-aware selection")

selected = run_tool(
    "data_select",
    data=scored,
    dataset_name="alpaca_data",
    budget=500,
    n_clusters=100,
    strategy="proportional",
    output_dir="outputs/alpaca_data_500"
)

log_step(f"Selected {len(selected)} records")

save_result(selected)
```

### CLI Examples
```bash
# End-to-end scoring + selection
elf run "score the alpaca data with dataelf, then give me the best 50" \
  -c config.yaml -v
```


## Configuration

Default parameters are configured in `tools/select/defaults.yaml`:

```yaml
budget: 9000
n_clusters: 100
strategy: proportional

embeddings_dir: outputs/embeddings
embedding_model: <path-to-embedding-model>
embedding_batch_size: 64
embedding_max_length: 1024
embedding_device: cuda
embedding_dtype: bfloat16

kmeans:
  backend: sklearn
  max_iter: 100
  n_init: 3
```

## Dependencies

| Dependency | Purpose | Required |
|------------|---------|----------|
| `torch` + `transformers` | Embedding extraction | Required when no cache available |
| GPU | Accelerate embedding extraction and clustering | Required when no cache available |
| `numpy` | Embedding storage and computation | Yes |
| `scikit-learn` | K-means clustering | Yes |
