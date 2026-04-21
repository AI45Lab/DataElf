"""
ProteinAnalyzerTool 集成测试，全部真实运行，不 mock。
运行方式：
    pytest test/tools/scitools/bio/test_protein_analyzer_tool.py -v
"""
import json
import logging
import os
from pathlib import Path

import pandas as pd
import pytest

from tools.scitools.bio.protein_analyzer_tool import ProteinAnalyzerTool

RESOURCE_DIR = Path(__file__).parent.parent.parent.parent / "resource" / "bio" / "protein_analyzer"

EXPECTED_COLUMNS = [
    "uniprot_id", "protein_name", "seq_length",
    "mw", "pI", "instability", "is_stable", "gravy",
    "helix_frac", "turn_frac", "sheet_frac", "aa_composition",
    "local_status", "n_blast_hits", "top_hit_id",
    "top_identity", "top_evalue", "top_title", "blast_status",
]

EXPECTED_EGFR = {
    "uniprot_id":   "P00533",
    "protein_name": "Epidermal growth factor receptor",
    "min_seq_len":  1000,
    "mw_range":     (130_000, 140_000),
    "pI_range":     (6.0, 7.0),
}


class MockContext:
    job_id = "test-protein"
    logger = logging.getLogger("test")

    def __init__(self, output_dir: str) -> None:
        self.config = {"output_dir": output_dir}

    def log(self, msg: str, level: str = "info") -> None:
        print(f"[{level}] {msg}")


def _load(filename: str):
    with open(RESOURCE_DIR / filename) as f:
        return json.load(f)


def _fmt(actual, expected=None) -> str:
    """Format actual value with optional expected annotation."""
    if expected is None:
        return str(actual)
    ok = "✓" if actual == expected else "✗"
    return f"{actual}  {ok} (expected: {expected})"


def _print_result(result: dict, label: str = "",
                  exp_status: str = "success",
                  exp_n_records: int = None,
                  exp_n_errors: int = 0,
                  exp_n_skipped: int = 0) -> None:
    r = result.get("result") or {}
    m = result.get("metadata") or {}
    a = result.get("artifacts") or {}
    tag = f" ({label})" if label else ""
    print(f"\n--- result{tag} ---")
    print(f"  status      : {_fmt(m.get('status'), exp_status)}"
          f"  (success=all ok, partial_success=some failed, error=nothing produced)")
    print(f"  n_records   : {_fmt(r.get('n_records'), exp_n_records)}  (rows written to output file)")
    print(f"  n_errors    : {_fmt(r.get('n_errors'), exp_n_errors)}  (records that threw exceptions)")
    print(f"  n_skipped   : {_fmt(r.get('n_skipped'), exp_n_skipped)}  (filtered at input validation)")
    print(f"  duration_ms : {m.get('duration_ms')} ms")
    print(f"  output_file : {a.get('output_file')}")


def _print_summary(summary: dict, label: str = "") -> None:
    tag = f" ({label})" if label else ""
    print(f"\n--- summary{tag} ---")
    print(f"  n_local_success : {summary.get('n_local_success')}  (BioPython analysis succeeded)")
    print(f"  n_local_error   : {summary.get('n_local_error')}  (BioPython analysis failed)")
    print(f"  pct_stable      : {summary.get('pct_stable')}%  "
          f"(instability index < 40 = stable; EGFR > 40 = unstable in vitro)")
    print(f"  mean_seq_length : {summary.get('mean_seq_length')} aa")
    print(f"  mean_mw         : {summary.get('mean_mw')} Da  "
          f"(small protein < 30 kDa, large > 100 kDa)")
    print(f"  mean_pI         : {summary.get('mean_pI')}  "
          f"(< 7 = acidic protein, > 7 = basic protein)")
    print(f"  mean_gravy      : {summary.get('mean_gravy')}  "
          f"(negative = hydrophilic, positive = hydrophobic/membrane protein)")
    if "n_blast_success" in summary:
        print(f"  n_blast_success    : {summary.get('n_blast_success')}  "
              f"(BLAST homology search succeeded)")
        print(f"  n_blast_hits_total : {summary.get('n_blast_hits_total')}  "
              f"(total homologous sequences found)")


@pytest.fixture
def tool():
    return ProteinAnalyzerTool()


@pytest.fixture
def tmp_output(tmp_path):
    return str(tmp_path)


@pytest.fixture
def sequences():
    return _load("sequences.json")


# Metadata tests

class TestMeta:

    def test_name(self, tool):
        print(f"\n--- tool.name ---")
        print(f"  actual  : {tool.name}  (expected: protein_analyzer)")
        assert tool.name == "protein_analyzer"

    def test_description_not_empty(self, tool):
        print(f"\n--- tool.description ---")
        print(f"  {tool.description}")
        assert len(tool.description) > 0

    def test_parameters_schema(self, tool):
        params = tool.parameters
        print(f"\n--- parameters schema ---")
        print(f"  type      : {params['type']}  (expected: object)")
        print(f"  required  : {params['required']}  (expected: ['data'])")
        print(f"  properties: {list(params['properties'].keys())}")
        assert params["type"] == "object"
        assert "data" in params["properties"]
        assert "data" in params["required"]

    def test_get_schema(self, tool):
        schema = tool.get_schema()
        print(f"\n--- tool schema keys ---")
        print(f"  keys     : {list(schema.keys())}")
        print(f"  has name : {'name' in schema}  (expected: True)")
        print(f"  has desc : {'description' in schema}  (expected: True)")
        print(f"  has params: {'parameters' in schema}  (expected: True)")
        assert {"name", "description", "parameters"}.issubset(schema)


# Validation tests

class TestValidation:

    def test_empty_data_returns_error(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output), data=[])
        print(f"\n--- empty data error ---")
        print(f"  result   : {result['result']}  (expected: None)")
        print(f"  metadata : {result['metadata']}")
        assert result["result"] is None
        assert "error" in result["metadata"]

    def test_missing_data_returns_error(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output))
        print(f"\n--- missing data error ---")
        print(f"  result   : {result['result']}  (expected: None)")
        print(f"  metadata : {result['metadata']}")
        assert result["result"] is None
        assert "error" in result["metadata"]

    def test_short_sequence_skipped(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=[{"id": "short", "sequence": "ACDE"}])
        print(f"\n--- short sequence validation ---")
        print(f"  input  : sequence='ACDE' (len=4, min required=10)")
        print(f"  result : {result['result']}  (expected: None)")
        assert result["result"] is None

    def test_invalid_aa_skipped(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=[{"id": "bad", "sequence": "ACDEFGHIKLMNPQRSTVWY1234"}])
        print(f"\n--- invalid amino acid chars ---")
        print(f"  input  : contains '1234' (not valid AA single-letter codes)")
        print(f"  result : {result['result']}  (expected: None)")
        assert result["result"] is None


# Single sequence tests

class TestSingleSequence:

    def test_returns_success(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        _print_result(result, "EGFR P00533",
                      exp_status="success", exp_n_records=1,
                      exp_n_errors=0, exp_n_skipped=0)
        assert result["result"] is not None
        assert result["metadata"]["status"] == "success"

    def test_expected_uniprot_id_in_output(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        print(f"\n--- uniprot_id check ---")
        print(f"  expected : {EXPECTED_EGFR['uniprot_id']}")
        print(f"  actual   : {list(df['uniprot_id'].values)}")
        print(f"  match    : {EXPECTED_EGFR['uniprot_id'] in df['uniprot_id'].values}  (expected: True)")
        assert EXPECTED_EGFR["uniprot_id"] in df["uniprot_id"].values

    def test_expected_protein_name(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        row = df[df["uniprot_id"] == EXPECTED_EGFR["uniprot_id"]].iloc[0]
        print(f"\n--- protein_name check ---")
        print(f"  expected : {EXPECTED_EGFR['protein_name']}")
        print(f"  actual   : {row['protein_name']}")
        print(f"  match    : {row['protein_name'] == EXPECTED_EGFR['protein_name']}  (expected: True)")
        assert row["protein_name"] == EXPECTED_EGFR["protein_name"]

    def test_physicochemical_properties_plausible(self, tool, tmp_output, sequences):
        """
        Field reference for reviewers:
          seq_length  — number of amino acid residues
          mw          — molecular weight in Daltons (EGFR ~134 kDa)
          pI          — isoelectric point; < 7 = acidic, > 7 = basic
          instability — instability index; < 40 = stable (EGFR > 40 = unstable in vitro)
          is_stable   — bool derived from instability index
          gravy       — hydropathicity; negative = hydrophilic, positive = hydrophobic
          helix/turn/sheet_frac — predicted secondary structure fractions, each in [0, 1]
          local_status — "success" if BioPython analysis completed without error
        """
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        row = df[df["uniprot_id"] == EXPECTED_EGFR["uniprot_id"]].iloc[0]
        lo_mw, hi_mw = EXPECTED_EGFR["mw_range"]
        lo_pi, hi_pi = EXPECTED_EGFR["pI_range"]

        print(f"\n--- physicochemical properties "
              f"({EXPECTED_EGFR['uniprot_id']}: {EXPECTED_EGFR['protein_name']}) ---")
        print(f"  seq_length  : {row['seq_length']} aa  "
              f"(expected: >= {EXPECTED_EGFR['min_seq_len']})")
        print(f"  mw          : {row['mw']:.2f} Da  "
              f"(expected: {lo_mw/1000:.0f}-{hi_mw/1000:.0f} kDa for EGFR)")
        print(f"  pI          : {row['pI']:.2f}  "
              f"(expected: {lo_pi}-{hi_pi}; < 7 = acidic)")
        print(f"  instability : {row['instability']:.2f}  "
              f"(< 40 = stable; EGFR > 40 = unstable in vitro)")
        print(f"  is_stable   : {row['is_stable']}  (expected: False for EGFR)")
        print(f"  gravy       : {row['gravy']:.3f}  (expected: negative = hydrophilic)")
        print(f"  helix_frac  : {row['helix_frac']:.3f}  (expected: in [0, 1])")
        print(f"  turn_frac   : {row['turn_frac']:.3f}  (expected: in [0, 1])")
        print(f"  sheet_frac  : {row['sheet_frac']:.3f}  (expected: in [0, 1])")
        print(f"  local_status: {row['local_status']}  (expected: success)")

        assert row["seq_length"] >= EXPECTED_EGFR["min_seq_len"]
        assert lo_mw <= row["mw"] <= hi_mw, f"mw={row['mw']} not in [{lo_mw}, {hi_mw}]"
        assert lo_pi <= row["pI"] <= hi_pi, f"pI={row['pI']} not in [{lo_pi}, {hi_pi}]"
        assert 0 <= row["helix_frac"] <= 1
        assert 0 <= row["sheet_frac"] <= 1
        assert 0 <= row["turn_frac"]  <= 1
        assert row["local_status"] == "success"

    def test_blast_skipped_when_off(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        print(f"\n--- blast_status when run_blast=False ---")
        print(f"  blast_status  : {df.iloc[0]['blast_status']}  (expected: skipped)")
        print(f"  blast_enabled : {result['metadata']['blast_enabled']}  (expected: False)")
        assert df.iloc[0]["blast_status"] == "skipped"
        assert result["metadata"]["blast_enabled"] is False


# Output schema tests

class TestOutputSchema:

    def test_column_names(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        print(f"\n--- output columns ---")
        print(f"  actual  : {sorted(df.columns)}")
        print(f"  expected: {sorted(EXPECTED_COLUMNS)}")
        print(f"  match   : {set(df.columns) == set(EXPECTED_COLUMNS)}  (expected: True)")
        assert set(df.columns) == set(EXPECTED_COLUMNS), (
            f"Column mismatch.\n  Expected: {sorted(EXPECTED_COLUMNS)}\n  Got: {sorted(df.columns)}")

    def test_output_file_exists(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        path = result["artifacts"]["output_file"]
        print(f"\n--- output file ---")
        print(f"  path        : {path}")
        print(f"  exists      : {os.path.exists(path)}  (expected: True)")
        print(f"  ends .parquet: {path.endswith('.parquet')}  (expected: True)")
        assert os.path.exists(path)
        assert path.endswith(".parquet")


# Result structure tests

class TestResultStructure:

    def test_summary_fields_present(self, tool, tmp_output, sequences):
        """
        Field reference for reviewers:
          n_local_success  — sequences successfully analyzed by BioPython
          n_local_error    — sequences that failed local analysis
          pct_stable       — % proteins with instability index < 40 (stable in vitro)
          mean_seq_length  — average sequence length (residues)
          mean_mw          — average molecular weight in Daltons
          mean_pI          — average isoelectric point
          mean_gravy       — average hydropathicity; negative = hydrophilic batch
        """
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        _print_result(result, "EGFR P00533",
                      exp_status="success", exp_n_records=1,
                      exp_n_errors=0, exp_n_skipped=0)
        _print_summary(result["result"]["summary"], "EGFR P00533")
        summary = result["result"]["summary"]
        for field in ("n_local_success", "n_local_error", "pct_stable",
                      "mean_seq_length", "mean_mw", "mean_pI", "mean_gravy"):
            assert field in summary, f"Missing summary field: {field}"

    def test_summary_no_blast_fields_when_off(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        summary = result["result"]["summary"]
        print(f"\n--- summary keys when run_blast=False ---")
        print(f"  keys                    : {sorted(summary.keys())}")
        print(f"  n_blast_success absent  : {'n_blast_success' not in summary}  (expected: True)")
        print(f"  n_blast_hits_total absent: {'n_blast_hits_total' not in summary}  (expected: True)")
        assert "n_blast_success" not in summary
        assert "n_blast_hits_total" not in summary

    def test_summary_counts_consistent(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        r, s = result["result"], result["result"]["summary"]
        total = s["n_local_success"] + s["n_local_error"]
        print(f"\n--- summary count consistency ---")
        print(f"  n_local_success + n_local_error = "
              f"{s['n_local_success']} + {s['n_local_error']} = {total}  "
              f"(expected: == n_records={r['n_records']})")
        print(f"  pct_stable = {s['pct_stable']}%  (expected: in [0.0, 100.0])")
        assert total == r["n_records"]
        assert 0.0 <= (s["pct_stable"] or 0.0) <= 100.0

    def test_metadata_fields_present(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        m = result["metadata"]
        print(f"\n--- metadata ---")
        print(f"  status           : {m.get('status')}  (expected: success)")
        print(f"  records_processed: {m.get('records_processed')}  (expected: 1)")
        print(f"  n_errors         : {m.get('n_errors')}  (expected: 0)")
        print(f"  blast_enabled    : {m.get('blast_enabled')}  (expected: False)")
        print(f"  duration_ms      : {m.get('duration_ms')} ms  (expected: > 0)")
        for field in ("status", "records_processed", "n_errors", "blast_enabled", "duration_ms"):
            assert field in m, f"Missing metadata field: {field}"
        assert m["duration_ms"] > 0

    def test_no_errors_on_clean_input(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=False)
        r = result["result"]
        print(f"\n--- error check on clean input ---")
        print(f"  errors : {r['errors']}  (expected: [])")
        print(f"  status : {result['metadata']['status']}  (expected: success)")
        assert r["errors"] == []
        assert result["metadata"]["status"] == "success"


# Multiple sequence tests

class TestMultipleSequences:

    def test_all_sequences_processed(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=sequences, run_blast=False)
        _print_result(result, f"{len(sequences)} sequences",
                      exp_status="success", exp_n_records=len(sequences),
                      exp_n_errors=0, exp_n_skipped=0)
        _print_summary(result["result"]["summary"], f"{len(sequences)} sequences")
        assert result["result"]["n_records"] == len(sequences)

    def test_all_uniprot_ids_in_output(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=sequences, run_blast=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        expected_ids = {r["uniprot_id"] for r in sequences}
        actual_ids   = set(df["uniprot_id"].values)
        print(f"\n--- uniprot_id coverage ({len(sequences)} sequences) ---")
        print(f"  expected : {sorted(expected_ids)}")
        print(f"  actual   : {sorted(actual_ids)}")
        print(f"  missing  : {expected_ids - actual_ids}  (expected: empty set)")
        assert expected_ids == actual_ids, f"Missing IDs: {expected_ids - actual_ids}"

    def test_duplicate_id_skipped(self, tool, tmp_output, sequences):
        duped = [sequences[0], sequences[0]]
        result = tool.run(MockContext(tmp_output), data=duped, run_blast=False)
        r = result["result"]
        print(f"\n--- duplicate id handling ---")
        print(f"  input count : {len(duped)}  (same record twice)")
        print(f"  n_records   : {r['n_records']}  (expected: 1, duplicate removed)")
        print(f"  n_skipped   : {r['n_skipped']}  (expected: 1)")
        assert r is not None
        assert r["n_records"] == 1
        assert r["n_skipped"] == 1


# Partial success tests

class TestPartialSuccess:

    def test_partial_success_when_one_record_fails(self, tool, tmp_output, sequences):
        """Inject one all-X record alongside a valid one to trigger potential partial_success."""
        bad = {"id": "bad_seq", "sequence": "X" * 20}
        data = [sequences[0], bad]
        result = tool.run(MockContext(tmp_output), data=data, run_blast=False)
        _print_result(result, "1 valid + 1 all-X sequence",
                      exp_status="success or partial_success")
        print(f"  errors : {result['result']['errors']}  "
              f"(non-empty if all-X caused BioPython exception)")
        assert result["result"] is not None
        assert result["metadata"]["status"] in ("success", "partial_success")

    def test_full_success_status_on_clean_batch(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=sequences[:3], run_blast=False)
        _print_result(result, "3 clean sequences",
                      exp_status="success", exp_n_records=3,
                      exp_n_errors=0, exp_n_skipped=0)
        assert result["metadata"]["status"] == "success"
        assert result["result"]["n_errors"] == 0


# BLAST tests (slow / network)

@pytest.mark.slow
@pytest.mark.network
class TestBlast:

    def test_blast_returns_hits(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=True)
        assert result["result"] is not None
        df = pd.read_parquet(result["artifacts"]["output_file"])
        row = df.iloc[0]
        print(f"\n--- BLAST results (P00533) ---")
        print(f"  blast_status : {row['blast_status']}  (expected: success)")
        print(f"  n_blast_hits : {row['n_blast_hits']}  (expected: > 0; homologous sequences found)")
        print(f"  top_hit_id   : {row['top_hit_id']}")
        print(f"  top_identity : {row['top_identity']}%  (sequence similarity to top hit)")
        print(f"  top_evalue   : {row['top_evalue']}  (expected: < 1e-5; smaller = more significant)")
        print(f"  top_title    : {row['top_title']}")
        assert row["blast_status"] == "success"
        assert row["n_blast_hits"] > 0
        assert row["top_evalue"] < 1e-5

    def test_blast_summary_fields_present(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=True)
        _print_summary(result["result"]["summary"], "BLAST enabled")
        summary = result["result"]["summary"]
        print(f"  n_blast_success present   : {'n_blast_success' in summary}  (expected: True)")
        print(f"  n_blast_hits_total present: {'n_blast_hits_total' in summary}  (expected: True)")
        assert "n_blast_success" in summary
        assert "n_blast_hits_total" in summary

    def test_blast_metadata_enabled(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=True)
        print(f"\n--- metadata (BLAST on) ---")
        print(f"  blast_enabled : {result['metadata']['blast_enabled']}  (expected: True)")
        assert result["metadata"]["blast_enabled"] is True

    def test_blast_top_hit_matches_egfr(self, tool, tmp_output, sequences):
        result = tool.run(MockContext(tmp_output), data=[sequences[0]], run_blast=True)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        top_title = df.iloc[0]["top_title"]
        print(f"\n--- BLAST top hit title check ---")
        print(f"  top_title        : {top_title}")
        print(f"  expected keywords: ['epidermal growth factor', 'egfr', 'receptor']")
        assert any(kw in top_title.lower() for kw in ["epidermal growth factor", "egfr", "receptor"]), (
            f"Top BLAST hit title unexpected: {top_title}")
