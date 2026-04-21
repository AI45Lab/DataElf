# protein_analyzer

## Overview

A protein sequence physicochemical analysis tool based on **BioPython ProteinAnalysis** for local computation, with optional **NCBI BLAST** homology search. Supports multiple input formats, outputs a standardized Parquet table, and returns a batch-level analytical summary.

Use cases:

- Batch computation of molecular weight, isoelectric point, stability, and hydrophobicity
- Secondary structure fraction estimation (α-helix / β-sheet / turn)
- Amino acid composition statistics
- Optional BLAST homology search (requires network; ~30–60 s per sequence)
- Downstream pipeline component for `enzyme_acquire`: accepts its Parquet output file path directly as input

## Input Schema

The `data` field accepts either a **list of dicts** or a **file path string** from a previous `enzyme_acquire` call:

### Option A — list of dicts

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `string` | ✅ | Unique protein identifier (e.g. UniProt ID) |
| `sequence` | `string` | ✅ | Single-letter amino acid sequence (case-insensitive; auto-uppercased) |
| `protein_name` | `string` | ❌ | Protein name; passed through to output |

Sequence constraints: minimum 10 amino acids; only standard single-letter codes `ACDEFGHIKLMNPQRSTVWYX` (X = unknown); duplicates filtered by `id` within a batch.

### Option B — file path string (for chaining with enzyme_acquire)

Pass `result["artifacts"]["output_file"]` from an `enzyme_acquire` call directly as `data`. The tool reads the Parquet file and extracts the `sequence` column automatically.

**Input sample (Option A):**

```json
[
  {
    "id": "P00533",
    "sequence": "MRPSGTAGAALLALLAALCPASRALEEKKVCQGTSNKLTQLGTFEDHFLSLQRMFNN...",
    "protein_name": "Epidermal growth factor receptor"
  }
]
```

**Input sample (Option B — chaining):**

```python
enzyme_result = run_tool("enzyme_acquire", data=["P00533"], fetch_smiles=False)
result = run_tool(
    "protein_analyzer",
    data=enzyme_result["artifacts"]["output_file"],  # pass file path directly
    run_blast=False,
)
```

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `data` | `list[dict]` or `str` | ✅ | — | Sequence list or path to an `enzyme_acquire` output Parquet file |
| `run_blast` | `bool` | ❌ | `false` | Whether to run NCBI BLAST homology search (requires network) |

## Output

### result

#### Statistics

| Field | Type | Description |
|-------|------|-------------|
| `n_records` | `int` | Total number of successfully analyzed records |
| `n_errors` | `int` | Records that raised exceptions during analysis |
| `n_skipped` | `int` | Records filtered out during input validation (sequence too short, invalid characters, duplicate ID) |
| `columns` | `list[str]` | Column names of the output table |
| `errors` | `list[dict]` | Failure details per record (contains `id`, `error`, `traceback`) |

#### Analytical summary (summary)

| Field | Type | Description |
|-------|------|-------------|
| `n_local_success` | `int` | Records successfully analyzed by BioPython |
| `n_local_error` | `int` | Records that failed local analysis |
| `pct_stable` | `float` | Percentage of stable proteins (%). A protein is considered stable when instability index < 40; the index is computed from dipeptide composition |
| `mean_seq_length` | `float` | Average sequence length (residues) |
| `mean_mw` | `float` | Average molecular weight (Da). Typical range: small protein < 30 kDa, large protein > 100 kDa |
| `mean_pI` | `float` | Average isoelectric point (pH). pI < 7 = acidic protein; pI > 7 = basic protein; affects solubility at different pH values |
| `mean_gravy` | `float` | Average GRAVY (Grand Average of Hydropathicity). Negative = hydrophilic; positive = hydrophobic (common in membrane proteins) |
| `n_blast_success` | `int` | Records with successful BLAST hits (only present when `run_blast=true`) |
| `n_blast_hits_total` | `int` | Total BLAST hits across all records (only present when `run_blast=true`) |

**Output sample (`run_blast=false`, input: EGFR P00533):**

```json
{
  "result": {
    "n_records": 1,
    "n_errors": 0,
    "n_skipped": 0,
    "columns": [
      "uniprot_id", "protein_name", "seq_length", "mw", "pI",
      "instability", "is_stable", "gravy", "helix_frac", "turn_frac",
      "sheet_frac", "aa_composition", "local_status",
      "n_blast_hits", "top_hit_id", "top_identity", "top_evalue",
      "top_title", "blast_status"
    ],
    "summary": {
      "n_local_success": 1,
      "n_local_error": 0,
      "pct_stable": 0.0,
      "mean_seq_length": 1210.0,
      "mean_mw": 134276.04,
      "mean_pI": 6.26,
      "mean_gravy": -0.316
    },
    "errors": []
  },
  "metadata": {
    "status": "success",
    "records_processed": 1,
    "n_errors": 0,
    "blast_enabled": false,
    "duration_ms": 70
  },
  "artifacts": {
    "output_file": "/tmp/sdc_outputs/bio/protein_analysis.parquet"
  }
}
```

> `aa_composition` is a dict of per-amino-acid fractions, e.g. `{"A": 0.072, "C": 0.018, "D": 0.053, ...}` (20 standard amino acids; truncated here).

### metadata

| Field | Type | Description |
|-------|------|-------------|
| `status` | `str` | `success` \| `partial_success` |
| `records_processed` | `int` | Same as `n_records` |
| `n_errors` | `int` | Same as `n_errors` |
| `blast_enabled` | `bool` | Whether BLAST search was enabled for this run |
| `duration_ms` | `float` | Total tool execution time (ms) |

### artifacts

| Field | Type | Description |
|-------|------|-------------|
| `output_file` | `str` | Absolute path to the output Parquet file |

### Output table schema

```
uniprot_id | protein_name | seq_length | mw | pI | instability | is_stable | gravy
helix_frac | turn_frac | sheet_frac | aa_composition | local_status
n_blast_hits | top_hit_id | top_identity | top_evalue | top_title | blast_status
```

### Status codes

| status | Meaning |
|--------|---------|
| `success` | All records analyzed successfully; `errors` is an empty list |
| `partial_success` | Some records raised exceptions, but usable results were written to the output file |
| `error` | No usable results (load failure / no valid sequences / all records failed) |

| error code | Meaning |
|------------|---------|
| `LOAD_FAILED` | Input loading failed (file not found, unsupported format, etc.) |
| `NO_SEQUENCES` | No valid sequences found after input validation |
| `ALL_FAILED` | All sequences raised exceptions during analysis |

## Example

```python
# Standalone usage: local physicochemical analysis, no BLAST
result = run_tool(
    "protein_analyzer",
    data=[
        {"id": "P00533", "sequence": "MRPSGTAGAA...", "protein_name": "EGFR"},
        {"id": "P68871", "sequence": "MVHLTPEEKS..."},
    ],
    run_blast=False,
)

# Analytical summary
summary = result["result"]["summary"]
print(f"Stable protein fraction: {summary['pct_stable']}%")  # instability index < 40
print(f"Mean molecular weight: {summary['mean_mw']} Da")
print(f"Mean isoelectric point: {summary['mean_pI']}")        # < 7 acidic, > 7 basic
print(f"Mean hydrophobicity: {summary['mean_gravy']}")        # negative = hydrophilic

# Inspect failure details when status is partial_success
if result["metadata"]["status"] == "partial_success":
    for err in result["result"]["errors"]:
        print(f"[{err['id']}] {err['error']}")

# Chaining with enzyme_acquire — pass the output file path directly
enzyme_result = run_tool("enzyme_acquire", data=["1.1.1.1"], fetch_smiles=False)
result = run_tool(
    "protein_analyzer",
    data=enzyme_result["artifacts"]["output_file"],  # str path accepted directly
    run_blast=False,
)
```

## Dependencies

- Local computation: `biopython` (`ProteinAnalysis`; no network required)
- BLAST: requires network access to NCBI BLAST API; ~30–60 s per sequence
- Internal modules: `tools.scitools.sources.biopython_protein` (`analyze_sequence`), `tools.scitools.sources.blast` (`run_blast`)