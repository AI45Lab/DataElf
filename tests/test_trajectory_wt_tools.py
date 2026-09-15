from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dataelf.domains.trajectory_analysis.tools.wt_serving.adapter import (
    COLUMNS,
    MAX_OUTPUT_BYTES,
    TRAJECTORY_COLUMNS,
    ServingAdapter,
    create_client,
    json_bytes,
)


@pytest.mark.parametrize("trace", [["synthetic", {"observation": None}], None, {"synthetic": [1, 2]}, "x" * 70000])
def test_domain_module_cli_with_synthetic_sdk(tmp_path, trace):
    """Run the real new module; only SDK I/O is replaced by synthetic dependencies."""
    root = Path(__file__).resolve().parents[1]
    sdk = tmp_path / "wt_sdk"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("")
    (sdk / "config.py").write_text(
        "from types import SimpleNamespace\n"
        "class GatewayConfig:\n"
        " def __init__(self): self.tables=SimpleNamespace(profile='test',serving_table='serving_test')\n")
    (sdk / "client.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "class WTGatewayClient:\n"
        " def __init__(self,config): self.config=config\n"
        " def query_data(self,**kwargs):\n"
        "  assert kwargs['table']=='serving_test' and kwargs['limit']==1\n"
        "  assert kwargs['deserialize_json'] is True and kwargs['exclude_none'] is False\n"
        "  with Path(os.environ['SYNTHETIC_CALLS']).open('a') as f: f.write(json.dumps(kwargs)+'\\n')\n"
        "  return json.loads(Path(os.environ['SYNTHETIC_ROWS']).read_text())\n"
        " def close(self): pass\n")
    dldb = tmp_path / "dldb"
    dldb.mkdir()
    (dldb / "__init__.py").write_text("")
    (dldb / "session.py").write_text("InformationSchemaTable=object\n")
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"id": "synthetic-id", "reward": 0, "chosen_trace": trace}]))
    env = {"HOME": str(tmp_path), "PYTHONPATH": str(tmp_path) + ":" + str(root),
           "PYTHONDONTWRITEBYTECODE": "1", "WT_SDK_PROFILE": "test",
           "WT_SDK_DB_URI": "synthetic", "WT_SDK_S3_ENDPOINT": "synthetic",
           "AWS_ACCESS_KEY_ID": "SYNTHETIC", "AWS_SECRET_ACCESS_KEY": "SYNTHETIC",
           "SYNTHETIC_ROWS": str(rows), "SYNTHETIC_CALLS": str(tmp_path / "calls.jsonl"),
           "WT_SERVING_TOOL_BINARY": "/nonexistent/old-cli-must-not-be-used"}
    module = "dataelf.domains.trajectory_analysis.tools.wt_serving.bridge"
    def call(tool, args):
        result = subprocess.run([sys.executable, "-B", "-m", module],
                                input=json.dumps({"tool": tool, "arguments": args}),
                                capture_output=True, text=True, env=env, cwd=tmp_path, timeout=15)
        assert result.returncode == 0 and result.stderr == ""
        assert len(result.stdout.encode()) <= 64 * 1024
        envelope = json.loads(result.stdout)
        assert envelope["isError"] is False
        return envelope["result"]
    search = call("wt_search_records", {"reward": 0, "limit": 1})
    assert search["records"][0]["reward"] == 0
    result = call("wt_get_record", {"record_id": search["records"][0]["id"], "fields": ["chosen_trace"]})
    if isinstance(trace, str):
        assert result["truncated"] is True
        assert result["omitted_fields"] == [{"record_index": 0, "field": "chosen_trace"}]
        assert "chosen_trace" not in result["records"][0]
    else:
        assert result["records"][0]["chosen_trace"] == trace
        assert result["truncated"] is False
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2 and "reward = 0" in calls[0]["filter_query"]
    assert "is_session_completed" not in calls[0]["filter_query"]
    assert "chosen_trace" in calls[1]["columns"] and "messages" not in calls[1]["columns"]


def test_import_domain_tool_does_not_load_sdk(tmp_path):
    root = Path(__file__).resolve().parents[1]
    probe = "import sys; from dataelf.domains.trajectory_analysis.tools.wt_serving import bridge; assert 'wt_sdk' not in sys.modules; print(bridge.__file__)"
    result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=tmp_path,
                            env={"PYTHONPATH": str(root), "HOME": str(tmp_path)},
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0
    assert Path(result.stdout.strip()) == root / "dataelf/domains/trajectory_analysis/tools/wt_serving/bridge.py"


@pytest.fixture
def client():
    return SimpleNamespace(
        config=SimpleNamespace(tables=SimpleNamespace(profile="test", serving_table="serving_test")),
        query_data=Mock(return_value=[{"id": "record-1", "step_id": 2, "messages": "secret"}]),
    )


@pytest.mark.parametrize("tool,args,expected_limit,expression", [
    ("wt_search_records", {}, 5, "id IS NOT NULL"),
    ("wt_search_records", {"job_id": "job-1", "session_id": "s1", "limit": 1}, 1,
     "job_id = 'job-1' AND session_id = 's1'"),
    ("wt_get_record", {"record_id": "record-1"}, 1, "id = 'record-1'"),

])
def test_queries(client, tool, args, expected_limit, expression):
    result = ServingAdapter(client).call(tool, args)
    kwargs = client.query_data.call_args.kwargs
    assert kwargs["table"] == "serving_test"
    assert kwargs["limit"] == expected_limit
    assert kwargs["filter_query"] == expression
    assert kwargs["columns"] == list(COLUMNS + TRAJECTORY_COLUMNS if tool == "wt_get_record" else COLUMNS)
    assert kwargs["deserialize_json"] is True
    assert result["records"][0]["id"] == "record-1"
    assert result["truncated"] is False
    if tool != "wt_get_record":
        assert "messages" not in result["records"][0]


@pytest.mark.parametrize("args", [
    {"table": "landing"}, {"endpoint": "x"}, {"credentials": "secret"}, {"sql": "SELECT 1"},
    {"filter_query": "id IS NOT NULL"}, {"limit": 0}, {"limit": 21}, {"limit": True},
    {"limit": 1.0}, {"limit": "1"}, {"reward": True}, {"reward": "0 OR 1=1"},
    {"job_id": "x\n"}, {"job_id": "x" * 1025}, [], None,
])
def test_reject_before_query(client, args):
    with pytest.raises((ValueError, TypeError)):
        ServingAdapter(client).call("wt_search_records", args)
    client.query_data.assert_not_called()


def test_recheck_config(client):
    adapter = ServingAdapter(client)
    client.config.tables.serving_table = "v2_landing_test"
    with pytest.raises(ValueError):
        adapter.call("wt_search_records", {})
    client.query_data.assert_not_called()


def test_profile(client):
    client.config.tables.profile = "production"
    with pytest.raises(ValueError):
        ServingAdapter(client)


def test_unknown_tool(client):
    with pytest.raises(KeyError):
        ServingAdapter(client).call("delete_serving", {})
    client.query_data.assert_not_called()


def test_projection_and_overreturn(client):
    client.query_data.return_value = [{"id": "sk-private", "reward": 0,
                                       "messages": {"password": "secret"}, "meta_json": "secret"}] * 50
    result = ServingAdapter(client).call("wt_search_records", {"limit": 1})
    assert len(result["records"]) == 1
    assert result["records"][0]["id"] == "sk-private"
    assert result["records"][0]["reward"] == 0
    assert result["truncated"] is True
    assert result["omitted_records"] == 49
    assert "secret" not in json.dumps(result)


def test_client_factory_validates_before_connect(monkeypatch):
    # Import is local only; no SDK client is initialized in this test.
    monkeypatch.setenv("WT_SDK_PROFILE", "production")
    with pytest.raises(ValueError):
        create_client()


@pytest.mark.parametrize("payload", [
    {"tool": "wt_search_records", "arguments": {"credentials": "CANARY_SECRET"}},
    {"tool": "delete_serving", "arguments": {}},
    {"tool": "wt_search_records", "arguments": {}, "endpoint": "CANARY_SECRET"},
    "x" * 9000,
])
def test_bridge_invalid_input(payload):
    import sys
    process = subprocess.run([sys.executable, "-m", "dataelf.domains.trajectory_analysis.tools.wt_serving.bridge"],
                             input=json.dumps(payload), text=True, capture_output=True, timeout=10, check=False)
    assert json.loads(process.stdout) == {"isError": True, "result": {"error": "WT_READ_FAILED"}}
    assert process.stderr == ""


def test_bridge_fake_success_and_close(monkeypatch, client, capfd):
    import io

    from dataelf.domains.trajectory_analysis.tools.wt_serving import bridge

    client.close = Mock()
    monkeypatch.setattr(bridge, "create_client", lambda: client)
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        b'{"tool":"wt_search_records","arguments":{"limit":1}}')))
    assert bridge.main() == 0
    captured = capfd.readouterr()
    assert json.loads(captured.out)["isError"] is False
    assert captured.err == ""
    client.close.assert_called_once()


def test_bridge_suppresses_sdk_error(monkeypatch, client, capfd):
    import io
    import os

    from dataelf.domains.trajectory_analysis.tools.wt_serving import bridge

    def fail(**kwargs):
        os.write(2, b"CANARY_SECRET")
        raise RuntimeError("CANARY_SECRET")

    client.query_data = fail
    client.close = Mock()
    monkeypatch.setattr(bridge, "create_client", lambda: client)
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        b'{"tool":"wt_search_records","arguments":{}}')))
    bridge.main()
    captured = capfd.readouterr()
    assert "CANARY_SECRET" not in captured.out + captured.err
    assert json.loads(captured.out)["isError"] is True
    client.close.assert_called_once()


def test_factory_rejects_serving_override(monkeypatch):
    import wt_sdk.client

    constructor = Mock()
    monkeypatch.setattr(wt_sdk.client, "WTGatewayClient", constructor)
    for key in ("WT_SDK_DB_URI", "WT_SDK_S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "fake")
    monkeypatch.setenv("WT_SDK_PROFILE", "test")
    monkeypatch.setenv("WT_SDK_SERVING_TABLE", "wind_tunnel_serving")
    with pytest.raises(ValueError):
        create_client()
    constructor.assert_not_called()


def test_schema_projection_matches_sdk():
    from wt_sdk.core.schemas import SERVING_SCHEMA

    assert set(COLUMNS + TRAJECTORY_COLUMNS) <= set(SERVING_SCHEMA.names)


def test_catalog_never_creates_or_lists(monkeypatch):
    import dldb.table

    from dataelf.domains.trajectory_analysis.tools.wt_serving.catalog import (
        ServingCatalog,
    )

    record = SimpleNamespace(table_name="serving_test")
    monkeypatch.setattr(dldb.table, "InformationSchemaRecord", lambda row: record)
    builder = Mock()
    builder.where.return_value = builder
    builder.limit.return_value = builder
    builder.to_arrow.return_value.to_pylist.return_value = [{"table_name": "serving_test"}]
    db = Mock()
    db.open_table.return_value.search.return_value = builder
    catalog = ServingCatalog(db)
    db.open_table.assert_called_once_with("information_schema")
    builder.where.assert_called_once_with("table_name = 'serving_test'")
    builder.limit.assert_called_once_with(1)
    db.create_table.assert_not_called()
    db.list_tables.assert_not_called()
    assert catalog.get("serving_test") is record
    with pytest.raises(ValueError):
        catalog.get("v2_landing_test")


def test_catalog_missing_fails_closed():
    from dataelf.domains.trajectory_analysis.tools.wt_serving.catalog import (
        ServingCatalog,
    )

    db = Mock()
    db.open_table.side_effect = RuntimeError("missing")
    with pytest.raises(RuntimeError):
        ServingCatalog(db)
    db.create_table.assert_not_called()


@pytest.mark.parametrize("reward", [0, 0.0, -1, 0.5, 2])
def test_reward_generic_and_zero(client, reward):
    ServingAdapter(client).call("wt_search_records", {"reward": reward, "limit": 1})
    kwargs = client.query_data.call_args.kwargs
    assert kwargs["filter_query"] == f"reward = {reward!r}"
    assert "is_session_completed" not in kwargs["filter_query"]


@pytest.mark.parametrize("reward", [float("nan"), float("inf"), -float("inf"), {}, []])
def test_invalid_reward(client, reward):
    with pytest.raises(ValueError):
        ServingAdapter(client).call("wt_search_records", {"reward": reward})
    client.query_data.assert_not_called()


def test_locator_literal_escaping(client):
    value = "_' OR 1=1 --" + "x" * 250
    ServingAdapter(client).call("wt_get_record", {"record_id": value})
    assert client.query_data.call_args.kwargs["filter_query"] == "id = '" + value.replace("'", "''") + "'"


def test_original_trajectory_structures(client):
    row = {"id": "raw-locator", "messages": [{"content": "test", "role": "user"}],
           "response": {"content": "test"}, "chosen_trace": [{"content": "test"}],
           "rejected_trace": None, "meta_json": {"nested": [1, None, {"x": "y"}]}}
    client.query_data.return_value = [row]
    result = ServingAdapter(client).call("wt_get_record", {"record_id": "raw-locator"})
    assert result["records"] == [row]
    assert "job_id" not in result["records"][0]  # Missing stays missing.
    assert result["truncated"] is False
    assert json.loads(json.dumps(result)) == result


def test_explicit_size_truncation_preserves_remaining_fields(client):
    row = {"id": "raw-locator", "messages": [{"content": "多" * MAX_OUTPUT_BYTES}],
           "response": {"content": "small"}, "chosen_trace": [], "rejected_trace": None}
    client.query_data.return_value = [row]
    result = ServingAdapter(client).call("wt_get_record", {"record_id": "raw-locator"})
    assert json_bytes(result) <= MAX_OUTPUT_BYTES
    assert result["truncated"] is True
    assert result["omitted_fields"] == [{"record_index": 0, "field": "messages"}]
    assert "messages" not in result["records"][0]
    assert result["records"][0]["response"] == row["response"]
    assert result["records"][0]["rejected_trace"] is None
    assert "messages" in row  # Original record is not mutated.


def test_oversized_metadata_is_explicit(client):
    client.query_data.return_value = [{"id": "x" * (MAX_OUTPUT_BYTES + 1)}]
    result = ServingAdapter(client).call("wt_search_records", {"limit": 1})
    assert result["records"] == []
    assert result["truncated"] is True
    assert result["omitted_records"] == 1
    assert json_bytes(result) <= MAX_OUTPUT_BYTES


def test_bridge_utf8_budget_matches_adapter(monkeypatch, client, capfd):
    import io

    from dataelf.domains.trajectory_analysis.tools.wt_serving import bridge

    client.query_data.return_value = [{"id": "r", "messages": [{"content": "多" * 20000}]}]
    client.close = Mock()
    monkeypatch.setattr(bridge, "create_client", lambda: client)
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        b'{"tool":"wt_get_record","arguments":{"record_id":"r"}}')))
    bridge.main()
    captured = capfd.readouterr()
    envelope = json.loads(captured.out)
    assert not envelope["isError"]
    assert not envelope["result"]["truncated"]
    assert len(captured.out.encode("utf-8")) <= MAX_OUTPUT_BYTES + 128


@pytest.mark.parametrize("fields", [["chosen_trace"], ["meta_json"],
                                    ["messages", "response", "rejected_trace"],
                                    ["chosen_trace", "chosen_trace"]])
def test_selected_fields_preserve_values(client, fields):
    row = {"id": "synthetic", "reward": 0, "messages": [None, {"x": []}],
           "response": {"nested": [None]}, "chosen_trace": [{"x": [None]}],
           "rejected_trace": None, "meta_json": {"x": [1, None]}}
    client.query_data.return_value = [row]
    result = ServingAdapter(client).call("wt_get_record", {"record_id": "synthetic", "fields": fields})
    selected = tuple(field for field in TRAJECTORY_COLUMNS if field in fields)
    kwargs = client.query_data.call_args.kwargs
    assert kwargs["columns"] == list(COLUMNS + selected)
    assert kwargs["deserialize_json"] is True
    assert kwargs["table"] == "serving_test"
    assert kwargs["limit"] == 1
    assert result["records"] == [{k: v for k, v in row.items() if k in COLUMNS + selected}]
    assert not result["truncated"]
    assert result["omitted_fields"] == []
    client.query_data.assert_called_once()


@pytest.mark.parametrize("fields", [[], None, "messages", ["id"], ["*"],
                                    ["chosen_trace AS x"], ["SELECT 1"], ["table"],
                                    [1], [True], [{}], ["messages"] * 6])
def test_fields_rejected_before_query(client, fields):
    with pytest.raises(ValueError):
        ServingAdapter(client).call("wt_get_record", {"record_id": "synthetic", "fields": fields})
    client.query_data.assert_not_called()


@pytest.mark.parametrize("field", ["chosen_trace", "meta_json"])
def test_single_field_budget(client, field):
    value = [{"content": "多" * MAX_OUTPUT_BYTES}]
    client.query_data.return_value = [{"id": "synthetic", field: value}]
    result = ServingAdapter(client).call("wt_get_record", {"record_id": "synthetic", "fields": [field]})
    assert result["records"] == [{"id": "synthetic"}]
    assert result["truncated"] is True
    assert result["omitted_fields"] == [{"record_index": 0, "field": field}]
    assert json_bytes(result) <= MAX_OUTPUT_BYTES
    assert client.query_data.return_value[0][field] is value


def test_selected_missing_is_not_null(client):
    client.query_data.return_value = [{"id": "synthetic", "rejected_trace": None}]
    result = ServingAdapter(client).call("wt_get_record", {
        "record_id": "synthetic", "fields": ["chosen_trace", "rejected_trace"]})
    assert result["records"] == [{"id": "synthetic", "rejected_trace": None}]
    assert result["omitted_fields"] == []


def test_cli_fields_roundtrip(monkeypatch, client, capfd):
    import io

    from dataelf.domains.trajectory_analysis.tools.wt_serving import bridge

    client.query_data.return_value = [{"id": "synthetic", "chosen_trace": [None, {"x": []}],
                                       "messages": ["not selected"]}]
    client.close = Mock()
    monkeypatch.setattr(bridge, "create_client", lambda: client)
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        b'{"tool":"wt_get_record","arguments":{"record_id":"synthetic","fields":["chosen_trace"]}}')))
    assert bridge.main() == 0
    captured = capfd.readouterr()
    envelope = json.loads(captured.out)
    assert not envelope["isError"]
    assert envelope["result"]["records"] == [{"id": "synthetic", "chosen_trace": [None, {"x": []}]}]
    assert captured.err == ""
    client.close.assert_called_once()
