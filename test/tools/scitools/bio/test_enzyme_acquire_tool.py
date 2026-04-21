"""
EnzymeAcquireTool 集成测试，全部真实调用，不 mock 网络。
运行方式：
    pytest test/tools/scitools/bio/test_enzyme_acquire_tool.py -v
"""
import json
import logging
import os
from pathlib import Path

import pandas as pd
import pytest

from tools.scitools.bio.enzyme_acquire_tool import EnzymeAcquireTool

RESOURCE_DIR = Path(__file__).parent.parent.parent.parent / "resource" / "bio" / "enzyme_acquire"

EXPECTED_COLUMNS = [
    "query", "input_type", "uniprot_id", "protein_name",
    "gene_name", "organism", "ec_number",
    "reactions", "substrates", "products", "pathways",
    "substrate_smiles", "sequence", "seq_length", "source_db",
]

EXPECTED_EC_1111 = {
    "input_type":           "ec_number",
    "ec_number_prefix":     "1.1.1.1",
    "protein_name_keyword": "dehydrogenase",
    "min_records":          1,
}

EXPECTED_A2RUC4 = {
    "uniprot_id":   "A2RUC4",
    "protein_name": "tRNA wybutosine-synthesizing protein 5",
    "input_type":   "uniprot_id",
    "min_seq_len":  100,
}


class MockContext:
    job_id = "test-enzyme"
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
                  exp_n_queries: int = None,
                  exp_n_succeeded: int = None,
                  exp_n_failed: int = 0,
                  exp_n_skipped: int = 0,
                  exp_n_records: int = None) -> None:
    r = result.get("result") or {}
    m = result.get("metadata") or {}
    a = result.get("artifacts") or {}
    tag = f" ({label})" if label else ""
    print(f"\n--- result{tag} ---")
    print(f"  status               : {_fmt(m.get('status'), exp_status)}  "
          f"(success=all ok, partial_success=some failed, error=nothing produced)")
    print(f"  n_queries_requested  : {_fmt(r.get('n_queries_requested'), exp_n_queries)}  "
          f"(total queries submitted)")
    print(f"  n_queries_succeeded  : {_fmt(r.get('n_queries_succeeded'), exp_n_succeeded)}  "
          f"(queries that returned results)")
    print(f"  n_queries_failed     : {_fmt(r.get('n_queries_failed'), exp_n_failed)}  "
          f"(queries with no results or network error)")
    print(f"  n_skipped            : {_fmt(r.get('n_skipped'), exp_n_skipped)}  "
          f"(filtered at input validation)")
    print(f"  n_records            : {_fmt(r.get('n_records'), exp_n_records)}  "
          f"(rows written to output file)")
    print(f"  n_duplicates_dropped : {r.get('n_duplicates_dropped')}  "
          f"(removed by uniprot_id dedup)")
    print(f"  duration_ms          : {m.get('duration_ms')} ms")
    print(f"  output_file          : {a.get('output_file')}")


def _print_summary(summary: dict, label: str = "") -> None:
    """Print summary fields with expected→actual annotations."""
    tag = f" ({label})" if label else ""
    print(f"\n--- summary{tag} ---")
    print(f"  n_with_sequence  : {summary.get('n_with_sequence')}  "
          f"(records with protein sequence; KEGG-only records typically have none)")
    print(f"  n_with_smiles    : {summary.get('n_with_smiles')}  "
          f"(records with substrate SMILES; only populated when fetch_smiles=True)")
    print(f"  n_with_reactions : {summary.get('n_with_reactions')}  "
          f"(records with catalytic reaction description; populated when fetch_kegg=True)")
    print(f"  pct_with_sequence: {summary.get('pct_with_sequence')}%")
    print(f"  pct_with_smiles  : {summary.get('pct_with_smiles')}%")
    print(f"  ec_class_dist    : {summary.get('ec_class_dist')}  "
          f"(1=oxidoreductase, 2=transferase, 3=hydrolase, "
          f"4=lyase, 5=isomerase, 6=ligase, 7=translocase)")
    print(f"  top_organisms    : {summary.get('top_organisms')}  "
          f"(top 5 species in results)")
    print(f"  source_db_dist   : {summary.get('source_db_dist')}  "
          f"(UniProt vs KEGG record distribution)")


@pytest.fixture
def tool():
    return EnzymeAcquireTool()


@pytest.fixture
def tmp_output(tmp_path):
    return str(tmp_path)


# Metadata tests  (no print — pure schema checks)

class TestMeta:

    def test_name(self, tool):
        print(f"\n--- tool.name ---\n  {tool.name}")
        assert tool.name == "enzyme_acquire"

    def test_description_not_empty(self, tool):
        print(f"\n--- tool.description ---\n  {tool.description}")
        assert len(tool.description) > 0

    def test_parameters_schema(self, tool):
        params = tool.parameters
        print(f"\n--- parameters schema ---")
        print(f"  required : {params['required']}")
        print(f"  properties: {list(params['properties'].keys())}")
        assert params["type"] == "object"
        assert "data" in params["properties"]
        assert "data" in params["required"]

    def test_get_schema(self, tool):
        schema = tool.get_schema()
        print(f"\n--- tool schema keys ---\n  {list(schema.keys())}")
        assert {"name", "description", "parameters"}.issubset(schema)


# Validation tests  (no print — error path checks)

class TestValidation:

    def test_empty_list_returns_error(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output), data=[])
        print(f"\n--- empty list error ---")
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

    def test_all_invalid_queries_blocked(self, tool, tmp_output):
        """All queries fail validation → error before any network call."""
        invalid = _load("error_cases.json")
        result = tool.run(MockContext(tmp_output), data=invalid)
        print(f"\n--- all invalid queries ---")
        print(f"  input    : {invalid}")
        print(f"  result   : {result['result']}  (expected: None)")
        print(f"  metadata : {result['metadata']}")
        assert result["result"] is None
        assert "error" in result["metadata"]

    def test_result_is_none_on_error(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output), data=["bad<>!"])
        print(f"\n--- invalid char query ---")
        print(f"  input    : ['bad<>!']  (contains invalid characters)")
        print(f"  result   : {result['result']}  (expected: None)")
        print(f"  metadata : {result['metadata']}")
        assert result["result"] is None


# EC number query tests

class TestECNumberQuery:

    def test_returns_success(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        _print_result(result, "EC 1.1.1.1",
                      exp_status="success", exp_n_queries=1,
                      exp_n_succeeded=1, exp_n_failed=0, exp_n_skipped=0)
        assert result["result"] is not None
        assert result["metadata"]["status"] in ("success", "partial_success")

    def test_expected_ec_number_in_output(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        actual = df["ec_number"].tolist()
        match = df["ec_number"].str.contains(EXPECTED_EC_1111["ec_number_prefix"], na=False).any()
        print(f"\n--- EC number check ---")
        print(f"  expected prefix : '{EXPECTED_EC_1111['ec_number_prefix']}'")
        print(f"  actual values   : {actual}")
        print(f"  prefix found    : {match}  (expected: True)")
        assert match, f"Expected EC {EXPECTED_EC_1111['ec_number_prefix']} in output"

    def test_expected_protein_name_keyword(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        keyword = EXPECTED_EC_1111["protein_name_keyword"]
        actual = df["protein_name"].tolist()
        match = df["protein_name"].str.lower().str.contains(keyword, na=False).any()
        print(f"\n--- protein_name keyword check ---")
        print(f"  expected keyword : '{keyword}'")
        print(f"  actual values    : {actual}")
        print(f"  keyword found    : {match}  (expected: True)")
        assert match, f"Expected at least one protein_name containing '{keyword}'"

    def test_input_type_classified_correctly(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        actual = df["input_type"].unique().tolist()
        print(f"\n--- input_type classification ---")
        print(f"  expected : '{EXPECTED_EC_1111['input_type']}'")
        print(f"  actual   : {actual}")
        assert (df["input_type"] == EXPECTED_EC_1111["input_type"]).all()

    def test_min_record_count(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        n = result["result"]["n_records"]
        print(f"\n--- record count ---")
        print(f"  expected : >= {EXPECTED_EC_1111['min_records']}")
        print(f"  actual   : {n}")
        assert n >= EXPECTED_EC_1111["min_records"]


# UniProt ID query tests

class TestUniProtIDQuery:

    def test_returns_success(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        _print_result(result, "UniProt A2RUC4",
                      exp_status="success", exp_n_queries=1,
                      exp_n_succeeded=1, exp_n_failed=0, exp_n_skipped=0)
        assert result["result"] is not None

    def test_expected_uniprot_id_in_output(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        actual = df["uniprot_id"].tolist()
        print(f"\n--- uniprot_id check ---")
        print(f"  expected : '{EXPECTED_A2RUC4['uniprot_id']}'")
        print(f"  actual   : {actual}")
        assert EXPECTED_A2RUC4["uniprot_id"] in df["uniprot_id"].values, (
            f"Expected {EXPECTED_A2RUC4['uniprot_id']} in output; got {actual}")

    def test_expected_protein_name(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        row = df[df["uniprot_id"] == EXPECTED_A2RUC4["uniprot_id"]].iloc[0]
        print(f"\n--- protein_name check ---")
        print(f"  expected : '{EXPECTED_A2RUC4['protein_name']}'")
        print(f"  actual   : '{row['protein_name']}'")
        assert row["protein_name"] == EXPECTED_A2RUC4["protein_name"], (
            f"Expected '{EXPECTED_A2RUC4['protein_name']}', got '{row['protein_name']}'"
        )

    def test_sequence_length_plausible(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        row = df[df["uniprot_id"] == EXPECTED_A2RUC4["uniprot_id"]].iloc[0]
        actual = len(str(row["sequence"]))
        print(f"\n--- sequence length check ---")
        print(f"  expected : >= {EXPECTED_A2RUC4['min_seq_len']} aa")
        print(f"  actual   : {actual} aa")
        assert actual >= EXPECTED_A2RUC4["min_seq_len"], (
            f"Expected seq_len >= {EXPECTED_A2RUC4['min_seq_len']}, got {actual}")

    def test_input_type_classified_correctly(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        actual = df["input_type"].unique().tolist()
        print(f"\n--- input_type classification ---")
        print(f"  expected : '{EXPECTED_A2RUC4['input_type']}'")
        print(f"  actual   : {actual}")
        assert (df["input_type"] == EXPECTED_A2RUC4["input_type"]).all()


# Enzyme name query tests

class TestEnzymeNameQuery:

    def test_returns_success(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("enzyme_name.json"),
                          fetch_smiles=False, fetch_kegg=False)
        _print_result(result, "enzyme name query",
                      exp_status="success", exp_n_queries=1,
                      exp_n_succeeded=1, exp_n_failed=0, exp_n_skipped=0)
        assert result["result"] is not None
        assert result["result"]["n_records"] > 0

    def test_target_protein_in_output(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("enzyme_name.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        actual = df["protein_name"].tolist()
        match = df["protein_name"].str.lower().str.contains("wybutosine", na=False).any()
        print(f"\n--- target protein check ---")
        print(f"  expected keyword : 'wybutosine'")
        print(f"  actual names     : {actual}")
        print(f"  keyword found    : {match}  (expected: True)")
        assert match, "Expected 'wybutosine' in at least one protein_name"


# Output schema tests

class TestOutputSchema:

    def test_parquet_column_names(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        print(f"\n--- output columns ---")
        print(f"  expected : {sorted(EXPECTED_COLUMNS)}")
        print(f"  actual   : {sorted(df.columns)}")
        assert set(df.columns) == set(EXPECTED_COLUMNS), (
            f"Column mismatch.\n  Expected: {sorted(EXPECTED_COLUMNS)}\n  Got: {sorted(df.columns)}")

    def test_parquet_no_null_uniprot_ids(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        uniprot_rows = df[df["source_db"].str.lower() == "uniprot"]
        empty_count = (uniprot_rows["uniprot_id"].str.len() == 0).sum()
        print(f"\n--- uniprot_id null check ---")
        print(f"  expected : 0 empty uniprot_id in UniProt-sourced rows")
        print(f"  actual   : {empty_count} empty  (total UniProt rows: {len(uniprot_rows)})")
        assert (uniprot_rows["uniprot_id"].str.len() > 0).all()

    def test_csv_output_extension(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, output_format="csv")
        path = result["artifacts"]["output_file"]
        print(f"\n--- CSV output format ---")
        print(f"  expected : file ending with .csv")
        print(f"  actual   : {path}")
        assert result["result"] is not None
        assert path.endswith(".csv")

    def test_output_file_exists_on_disk(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        path = result["artifacts"]["output_file"]
        exists = os.path.exists(path)
        print(f"\n--- output file on disk ---")
        print(f"  expected : file exists = True")
        print(f"  actual   : {path}  exists={exists}")
        assert exists


# Result structure tests

class TestResultStructure:

    def test_query_level_counts_present(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        r = result["result"]
        print(f"\n--- query-level counts ---")
        print(f"  n_queries_requested : expected=present  actual={r.get('n_queries_requested')}")
        print(f"  n_queries_succeeded : expected=present  actual={r.get('n_queries_succeeded')}")
        print(f"  n_queries_failed    : expected=present  actual={r.get('n_queries_failed')}")
        print(f"  n_skipped           : expected=present  actual={r.get('n_skipped')}")
        for field in ("n_queries_requested", "n_queries_succeeded",
                      "n_queries_failed", "n_skipped"):
            assert field in r, f"Missing result field: {field}"

    def test_query_counts_consistent(self, tool, tmp_output):
        queries = _load("ec_number.json")
        result = tool.run(MockContext(tmp_output), data=queries,
                          fetch_smiles=False, fetch_kegg=False)
        r = result["result"]
        total = r["n_queries_succeeded"] + r["n_queries_failed"]
        print(f"\n--- query count consistency ---")
        print(f"  expected : n_queries_succeeded + n_queries_failed <= n_queries_requested")
        print(f"  actual   : {r['n_queries_succeeded']} + {r['n_queries_failed']} "
              f"= {total} <= {r['n_queries_requested']}")
        assert total <= r["n_queries_requested"]
        assert r["n_queries_requested"] == len(queries)

    def test_summary_fields_present(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        _print_result(result, "EC 1.1.1.1",
                      exp_status="success", exp_n_queries=1,
                      exp_n_succeeded=1, exp_n_failed=0)
        _print_summary(result["result"]["summary"], "EC 1.1.1.1")
        summary = result["result"]["summary"]
        for field in ("n_with_sequence", "n_with_smiles", "n_with_reactions",
                      "pct_with_sequence", "ec_class_dist", "top_organisms",
                      "source_db_dist"):
            assert field in summary, f"Missing summary field: {field}"

    def test_summary_counts_consistent_with_n_records(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        r, s = result["result"], result["result"]["summary"]
        print(f"\n--- summary count consistency ---")
        print(f"  n_with_sequence  : expected <= n_records={r['n_records']}  "
              f"actual={s['n_with_sequence']}")
        print(f"  pct_with_sequence: expected in [0.0, 100.0]  "
              f"actual={s['pct_with_sequence']}%")
        assert s["n_with_sequence"] <= r["n_records"]
        assert 0.0 <= s["pct_with_sequence"] <= 100.0

    def test_metadata_fields_present(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=False)
        m = result["metadata"]
        print(f"\n--- metadata ---")
        for k, v in m.items():
            print(f"  {k} : expected=present  actual={v}")
        for field in ("records_processed", "n_errors", "status", "duration_ms"):
            assert field in m, f"Missing metadata field: {field}"
        assert m["duration_ms"] > 0

    def test_no_errors_on_clean_input(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        r = result["result"]
        print(f"\n--- error check on clean input ---")
        print(f"  errors : expected=[]  actual={r['errors']}")
        print(f"  status : expected=success  actual={result['metadata']['status']}")
        assert r["errors"] == []
        assert result["metadata"]["status"] == "success"

    def test_dedup_field_present(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("multiple_types.json"),
                          fetch_smiles=False, fetch_kegg=False)
        r = result["result"]
        print(f"\n--- dedup check ---")
        print(f"  n_duplicates_dropped : expected=present  "
              f"actual={r.get('n_duplicates_dropped')}  "
              f"(records removed by uniprot_id deduplication)")
        assert "n_duplicates_dropped" in r

    def test_max_results_respected(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          max_results=2, fetch_smiles=False, fetch_kegg=False)
        n = result["result"]["n_records"]
        print(f"\n--- max_results check ---")
        print(f"  expected : n_records <= 2  (max_results=2)")
        print(f"  actual   : n_records = {n}")
        assert n <= 2


# Partial success tests

class TestPartialSuccess:

    def test_partial_success_status_when_one_query_fails(self, tool, tmp_output):
        """One valid + one definitely-invalid ID → partial_success, not error."""
        result = tool.run(MockContext(tmp_output),
                          data=["1.1.1.1", "INVALID_ID_XXXXXX"],
                          fetch_smiles=False, fetch_kegg=False)
        _print_result(result, "1 valid + 1 invalid query",
                      exp_status="partial_success", exp_n_queries=2,
                      exp_n_succeeded=1, exp_n_failed=1, exp_n_skipped=0)
        print(f"\n--- partial_success status check ---")
        print(f"  expected : status = partial_success")
        print(f"  actual   : status = {result['metadata']['status']}")
        assert result["result"] is not None
        assert result["artifacts"]["output_file"] is not None
        assert result["metadata"]["status"] == "partial_success"

    def test_partial_success_counts_are_consistent(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=["1.1.1.1", "INVALID_ID_XXXXXX"],
                          fetch_smiles=False, fetch_kegg=False)
        r = result["result"]
        print(f"\n--- partial_success counts ---")
        print(f"  n_queries_succeeded : expected >= 1  actual={r['n_queries_succeeded']}")
        print(f"  n_queries_failed    : expected >= 1  actual={r['n_queries_failed']}")
        print(f"  len(errors)         : expected = n_queries_failed={r['n_queries_failed']}  "
              f"actual={len(r['errors'])}")
        print(f"  errors              : {[e['query'] + ' → ' + e['error'] for e in r['errors']]}")
        assert r["n_queries_failed"] >= 1
        assert r["n_queries_succeeded"] >= 1
        assert len(r["errors"]) == r["n_queries_failed"]

    def test_full_success_when_all_queries_return_results(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("uniprot_id.json"),
                          fetch_smiles=False, fetch_kegg=False)
        _print_result(result, "clean UniProt query",
                      exp_status="success", exp_n_queries=1,
                      exp_n_succeeded=1, exp_n_failed=0, exp_n_skipped=0)
        print(f"\n--- full success check ---")
        print(f"  status          : expected=success  actual={result['metadata']['status']}")
        print(f"  n_queries_failed: expected=0  actual={result['result']['n_queries_failed']}")
        print(f"  errors          : expected=[]  actual={result['result']['errors']}")
        assert result["metadata"]["status"] == "success"
        assert result["result"]["n_queries_failed"] == 0
        assert result["result"]["errors"] == []


# Slow / network-intensive tests
@pytest.mark.slow
@pytest.mark.network
class TestMixedAndBulkQueries:

    def test_mixed_input_types(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("mixed_types.json"),
                          fetch_smiles=False, fetch_kegg=False)
        _print_result(result, "mixed input types",
                      exp_status="success", exp_n_failed=0, exp_n_skipped=0)
        _print_summary(result["result"]["summary"], "mixed types")
        print(f"\n--- mixed types record count ---")
        print(f"  expected : n_records > 1")
        print(f"  actual   : n_records = {result['result']['n_records']}")
        assert result["result"] is not None
        assert result["result"]["n_records"] > 1

    def test_smiles_fetched_when_enabled(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=True, fetch_kegg=False)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        smiles_filled = (df["substrate_smiles"].str.len() > 0).sum()
        print(f"\n--- SMILES fetch check ---")
        print(f"  expected : substrate_smiles column present")
        print(f"  actual   : column present={('substrate_smiles' in df.columns)}  "
              f"rows with SMILES={smiles_filled}/{len(df)}  "
              f"(PubChem may not have all substrates)")
        assert "substrate_smiles" in df.columns

    def test_kegg_enrichment_adds_reactions(self, tool, tmp_output):
        result = tool.run(MockContext(tmp_output),
                          data=_load("ec_number.json"),
                          fetch_smiles=False, fetch_kegg=True)
        df = pd.read_parquet(result["artifacts"]["output_file"])
        reactions_filled = (df["reactions"].str.len() > 0).sum()
        print(f"\n--- KEGG enrichment check ---")
        print(f"  expected : reactions column present")
        print(f"  actual   : column present={('reactions' in df.columns)}  "
              f"rows with reactions={reactions_filled}/{len(df)}")
        assert "reactions" in df.columns
