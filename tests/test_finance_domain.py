from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import DiscoveryJob, DomainManifest, JobSpec
from dataelf.domains.finance.benchmarks import get_benchmark, list_benchmarks
from dataelf.domains.finance.config import FinanceDomainConfig
from dataelf.domains.finance.plugin import FinanceDomainPlugin, create_plugin
from dataelf.domains.finance.tools.sqlite_tools import _assert_read_only, _safe_identifier


EXPECTED_BENCHMARKS = [
    "dataclawbench",
    "ddr_10k",
    "finance_agent_bench_v1_1",
    "finance_agent_bench_v2",
    "finance_complex_qa",
    "finfirst",
    "finsearchcomp_t2",
    "finsearchcomp_t3",
    "frontier_finance",
]

MANIFEST = DomainManifest.model_validate({
    "domain": "finance",
    "version": "1",
    "display_name": "Finance Analysis",
    "plugin": "dataelf.domains.finance.plugin:create_plugin",
    "capabilities": ["structured_data_access", "financial_analysis"],
    "workspace_dirs": ["raw/web", "results"],
})


def _config(values: dict | None = None) -> DataElfConfig:
    config = DataElfConfig()
    return config.model_copy(update={"domains": {"finance": values or {}}})


def _plugin(values: dict | None = None) -> FinanceDomainPlugin:
    return create_plugin(_config(values), MANIFEST)


def _spec(objective: str = "Analyze the data", **parameters) -> JobSpec:
    return JobSpec(domain="finance", objective=objective, parameters=parameters)


def test_all_benchmark_configs_load_and_validate():
    assert list_benchmarks() == EXPECTED_BENCHMARKS
    for name in EXPECTED_BENCHMARKS:
        benchmark = get_benchmark(name)
        assert benchmark.name == name
        assert benchmark.folder.name == name


def test_unknown_benchmark_is_rejected():
    with pytest.raises(ValueError, match="Unknown Finance benchmark"):
        get_benchmark("nope_benchmark")
    with pytest.raises(ValueError):
        FinanceDomainConfig.from_mapping({"benchmark": "nope_benchmark"})


def test_benchmark_config_rejects_unknown_keys(tmp_path: Path):
    source = Path("dataelf/domains/finance/benchmarks/ddr_10k/config.yaml")
    payload = source.read_text(encoding="utf-8").replace("benchmark: ddr_10k", "bogus_key: 1\nbenchmark: ddr_10k", 1)
    target = tmp_path / "config.yaml"
    target.write_text(payload, encoding="utf-8")
    import yaml

    with pytest.raises(Exception):
        from dataelf.domains.finance.config import BenchmarkConfig

        BenchmarkConfig.model_validate({**yaml.safe_load(payload), "benchmark": "ddr_10k"})


def test_benchmark_prompt_fields_optional_with_generic_fallbacks(tmp_path: Path):
    # Development plan §4 principle 7: every prompt field must trace back to
    # original-benchmark text; a benchmark whose original has no counterpart
    # omits the field (or the whole section) and the generic prompt.py
    # fallbacks apply at render time.
    from dataelf.domains.finance.benchmarks import ResolvedBenchmark
    from dataelf.domains.finance.config import BenchmarkConfig
    from dataelf.domains.finance.plugin import _benchmark_prompt_profile

    config = BenchmarkConfig.model_validate({
        "benchmark": "minimal",
        "display_name": "Minimal",
        "tools": {"sql": True},
        "prompt": {"instructions": ["Only original text."]},
    })
    assert config.prompt.source_guidance is None
    profile = _benchmark_prompt_profile(ResolvedBenchmark(config=config, folder=tmp_path))
    assert "source_guidance" not in profile
    assert "tool_guidance" not in profile
    assert "output_guidance" not in profile
    assert "stopping_policy" not in profile
    assert profile["instructions"] == ["Only original text."]

    sectionless = BenchmarkConfig.model_validate({"benchmark": "bare", "display_name": "Bare"})
    assert sectionless.prompt.stopping_policy is None

    spec = JobSpec(domain="finance", objective="Analyze the data", parameters={"prompt_profile": profile})
    job = DiscoveryJob.model_validate({"job_id": "j_opt", "spec": spec.model_dump(), "status": "running", "workspace_path": "/tmp/ws", "artifacts": []})
    from dataelf.discovery.contracts import DiscoveryContext
    from dataelf.domains.finance.prompt import build_finance_prompt

    rendered = build_finance_prompt(
        job, DiscoveryContext(workspace_path="/tmp/ws", spec=spec, manifest=MANIFEST)
    )
    assert "Use the prepared source artifacts listed by the runtime" in rendered
    assert "Only original text." in rendered


def test_benchmark_output_fields_pin_the_required_set():
    # Benchmark-declared outputs.fields patch the default schema: the answer
    # carrier stays required, framework-only fields relax, and the benchmarks
    # whose originals ship a strict deliverable (DDR insights+final summary,
    # FinanceComplexQA {"thinking","answer"}) keep exactly that shape.
    from dataelf.domains.finance.artifacts import output_schema_for_spec

    plugin = _plugin()
    for name, required in [
        ("finsearchcomp_t2", ["summary"]),
        ("finsearchcomp_t3", ["summary"]),
        ("dataclawbench", ["summary"]),
        ("frontier_finance", ["summary"]),
        ("ddr_10k", ["summary", "key_findings"]),
        ("finance_complex_qa", ["summary", "thinking"]),
    ]:
        spec = plugin.normalize_spec(_spec(benchmark=name))
        schema = output_schema_for_spec(spec.parameters)
        assert [field.name for field in schema.fields if field.required] == required, name

    # One-answer benchmarks accept a bare-answer payload without warnings;
    # the list notation of DataClaw's multi-answer tasks lives inside the
    # final answer text, so summary stays a string.
    from dataelf.domains.finance.artifacts import validate_payload

    for name, summary in [
        ("finsearchcomp_t2", "$17956 billion"),
        ("dataclawbench", '["0.2356", "-0.1048", "0.34"]'),
    ]:
        spec = plugin.normalize_spec(_spec(benchmark=name))
        review = validate_payload(output_schema_for_spec(spec.parameters), {"summary": summary})
        assert review.warnings == [] and review.metrics == {"summary_present": True}, name


def test_modeling_section_is_reserved_placeholder():
    # `--no-modeling` writes modeling.enabled=false; it must parse and leave
    # the stage skipped (create_modeler returns None).
    assert FinanceDomainConfig.from_mapping({}).modeling.enabled is False
    config = FinanceDomainConfig.from_mapping({"modeling": {"enabled": False}})
    assert config.modeling.enabled is False
    plugin = _plugin({"modeling": {"enabled": False}})
    assert plugin.create_modeler(_spec(), _config()) is None


def test_modeling_enabled_fails_fast_as_unsupported():
    plugin = _plugin({"modeling": {"enabled": True}})
    with pytest.raises(ValueError, match="FINANCE_MODELING_UNSUPPORTED"):
        plugin.create_modeler(_spec(), _config())


def test_modeling_section_rejects_unknown_keys():
    with pytest.raises(ValueError):
        FinanceDomainConfig.from_mapping({"modeling": {"bogus": True}})


def test_generic_run_defaults(tmp_path: Path):
    plugin = _plugin()
    spec = plugin.normalize_spec(_spec())
    assert spec.constraints["max_runtime_minutes"] == 30
    assert spec.parameters["finance_output_fields"] == []
    assert spec.parameters["finance_tool_flags"] == {"sql": False, "python": True, "web": False, "edgar": False, "prices": False}
    assert spec.parameters["finance_tool_names"] == [
        "execute_code", "list_files", "get_field_description",
        "read", "bash", "edit", "write", "grep", "find", "ls",
    ]
    contract = plugin.output_contract(spec)
    assert [artifact.artifact_id for artifact in contract.artifacts] == ["finance_results", "finance_brief"]
    assert [artifact.path for artifact in contract.artifacts] == ["results/results.json", "results/final_brief.md"]


def test_finance_runtime_budget_is_domain_configurable():
    plugin = _plugin({"analysis": {"max_runtime_seconds": 3600}})
    spec = plugin.normalize_spec(_spec())
    assert spec.constraints["max_runtime_minutes"] == 60


def test_explicit_runtime_constraint_wins_over_domain_default():
    plugin = _plugin({"analysis": {"max_runtime_seconds": 3600}})
    spec = plugin.normalize_spec(_spec())
    spec = spec.model_copy(update={"constraints": {"max_runtime_minutes": 45}})
    normalized = plugin.normalize_spec(spec)
    assert normalized.constraints["max_runtime_minutes"] == 45


def test_benchmark_selection_resolves_tools():
    plugin = _plugin()
    spec = plugin.normalize_spec(_spec(benchmark="finsearchcomp_t2"))
    assert spec.parameters["finance_tool_flags"]["web"] is True
    assert spec.parameters["finance_tool_flags"]["sql"] is False
    assert "web_search" in spec.parameters["finance_tool_names"]
    assert "execute_query" not in spec.parameters["finance_tool_names"]
    assert spec.requested_outputs == ["finance_results", "finance_brief"]


def test_configured_benchmark_used_when_parameter_absent():
    plugin = _plugin({"benchmark": "dataclawbench"})
    spec = plugin.normalize_spec(_spec())
    assert spec.parameters["benchmark"] == "dataclawbench"
    # The benchmark's output-field overrides (relaxations of the default
    # schema) flow into the spec when no runtime parameter declares them.
    assert spec.parameters["finance_output_fields"] == [
        {"name": "key_findings", "required": False},
        {"name": "evidence_refs", "required": False},
        {"name": "confidence", "required": False},
        {"name": "limitations", "required": False},
    ]
    assert spec.parameters["finance_tool_flags"] == {"sql": False, "python": True, "web": False, "edgar": False, "prices": False}
    assert "bash" in spec.parameters["finance_tool_names"]


def test_operator_tool_overrides_beat_benchmark_declaration():
    plugin = _plugin({"benchmark": "finsearchcomp_t2", "tools": {"web": False}})
    spec = plugin.normalize_spec(_spec())
    assert spec.parameters["finance_tool_flags"]["web"] is False


def test_runtime_tool_overrides_beat_everything():
    plugin = _plugin({"benchmark": "ddr_10k"})
    spec = plugin.normalize_spec(_spec(finance_tools={"sql": False, "python": True, "web": True}))
    assert spec.parameters["finance_tool_flags"] == {"sql": False, "python": True, "web": True, "edgar": False, "prices": False}


def test_edgar_and_price_flags_flow_from_benchmark_declaration():
    plugin = _plugin()
    v1 = plugin.normalize_spec(_spec(benchmark="finance_agent_bench_v1_1"))
    assert v1.parameters["finance_tool_flags"]["edgar"] is True
    assert v1.parameters["finance_tool_flags"]["prices"] is False
    assert "edgar_search" in v1.parameters["finance_tool_names"]
    assert "parse_html_page" in v1.parameters["finance_tool_names"]
    assert "price_history" not in v1.parameters["finance_tool_names"]

    v2 = plugin.normalize_spec(_spec(benchmark="finance_agent_bench_v2"))
    assert v2.parameters["finance_tool_flags"]["edgar"] is True
    assert v2.parameters["finance_tool_flags"]["prices"] is True
    assert "price_history" in v2.parameters["finance_tool_names"]


def test_market_tools_gate_allowed_tools_and_pi_resources(tmp_path: Path):
    values = {"tools": {"sql": False, "python": False, "edgar": True, "prices": True}}
    plugin = _plugin(values)
    spec = plugin.normalize_spec(_spec())
    assert spec.parameters["finance_tool_names"] == [
        "edgar_search", "company_profile", "company_facts", "parse_html_page", "retrieve_information", "price_history",
        "read", "bash", "edit", "write", "grep", "find", "ls",
    ]
    resources = plugin.agent_resources(spec, _config(values))
    assert [path.name for path in resources.extensions] == ["finance_edgar_prices.ts"]
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert result.env["DATAELF_FINANCE_ALLOWED_TOOLS"].split(",") == ["edgar_search", "company_profile", "company_facts", "parse_html_page", "retrieve_information", "price_history"]


def test_prepare_files_source(tmp_path: Path):
    data = tmp_path / "source" / "input.csv"
    data.parent.mkdir(parents=True)
    data.write_text("ticker,close\nAAPL,180.5\n", encoding="utf-8")
    values = {"source": {"files": {"data_path": str(data)}}, "tools": {"sql": False}}
    plugin = _plugin(values)
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert result.context["source_mode"] == "files"
    assert result.context["source_kinds"] == ["files"]


def test_prepare_combines_sqlite_and_files_sources(tmp_path: Path):
    db = tmp_path / "source" / "10k.db"
    db.parent.mkdir(parents=True)
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    docs = tmp_path / "source" / "docs"
    docs.mkdir()
    (docs / "extra.csv").write_text("k,v\na,1\n", encoding="utf-8")
    values = {"benchmark": "ddr_10k", "source": {"sqlite": {"db_path": str(db)}, "files": {"data_path": str(docs)}}}
    plugin = _plugin(values)
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert result.context["source_mode"] == "mixed"
    assert result.context["source_kinds"] == ["sqlite", "files"]
    assert (tmp_path / "ws" / "tables" / "finance" / "finance.db").is_file()
    assert (tmp_path / "ws" / "raw" / "finance" / "input" / "extra.csv").is_file()
    assert "DATAELF_FINANCE_DB" in result.env


def test_prepare_links_sources_instead_of_copying(tmp_path: Path):
    import os
    import sqlite3

    db = tmp_path / "source" / "10k.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    docs = tmp_path / "source" / "docs"
    docs.mkdir()
    (docs / "extra.csv").write_text("k,v\na,1\n", encoding="utf-8")
    values = {
        "benchmark": "ddr_10k",
        "source": {
            "sqlite": {"db_path": str(db), "link": True},
            "files": {"data_path": str(docs), "link": True},
        },
    }
    plugin = _plugin(values)
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    workspace_db = tmp_path / "ws" / "tables" / "finance" / "finance.db"
    linked_csv = tmp_path / "ws" / "raw" / "finance" / "input" / "extra.csv"
    assert workspace_db.is_symlink() and workspace_db.resolve() == db.resolve()
    assert linked_csv.is_symlink() and linked_csv.resolve() == (docs / "extra.csv").resolve()
    manifest = json.loads((tmp_path / "ws" / "tables" / "finance" / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["linked"] is True
    assert manifest["provenance"]["database"]["strategy"] == "symlink"
    assert manifest["provenance"]["database"]["target"] == str(db.resolve())
    assert manifest["provenance"]["database"]["size"] > 0
    assert manifest["provenance"]["files"]["strategy"] == "symlink"
    assert result.context["linked"] is True
    assert set(result.authorized_outside) == {str(db.resolve()), str(docs.resolve())}
    assert set(result.env["DATAELF_FINANCE_PROTECTED"].split(os.pathsep)) == {str(db.resolve()), str(docs.resolve())}
    # schema inspection and the files tools keep working through the links
    assert (tmp_path / "ws" / "tables" / "finance" / "schema.json").is_file()
    from dataelf.domains.finance import tools

    for key, value in result.env.items():
        if key.startswith("DATAELF_FINANCE_"):
            os.environ[key] = value
    try:
        assert tools.dispatch("list_files", {})["files"] == ["extra.csv"]
        assert tools.dispatch("get_field_description", {"data_file": "extra.csv"})["fields"] == ["k", "v"]
    finally:
        for key in result.env:
            if key.startswith("DATAELF_FINANCE_"):
                os.environ.pop(key, None)


def test_prepare_links_single_file_source_under_real_input_dir(tmp_path: Path):
    data = tmp_path / "source" / "prices.csv"
    data.parent.mkdir(parents=True)
    data.write_text("ticker,close\nAAPL,180.5\n", encoding="utf-8")
    values = {"source": {"files": {"data_path": str(data), "link": True}}, "tools": {"sql": False}}
    plugin = _plugin(values)
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    input_dir = tmp_path / "ws" / "raw" / "finance" / "input"
    # input_dir stays a real directory (it is the execute_code cwd); only the
    # file inside is a link.
    assert input_dir.is_dir() and not input_dir.is_symlink()
    assert (input_dir / "prices.csv").is_symlink()


def test_link_and_readonly_flags_from_environment(monkeypatch):
    monkeypatch.setenv("DATAELF_FINANCE_DB_PATH", "/somewhere/finance.db")
    monkeypatch.setenv("DATAELF_FINANCE_DB_LINK", "1")
    monkeypatch.setenv("DATAELF_FINANCE_READONLY_CODE", "true")
    config = FinanceDomainConfig.from_mapping({})
    assert config.source.sqlite is not None and config.source.sqlite.link is True
    assert config.analysis.readonly_code is True


def test_readonly_code_setting_flows_to_run_env(tmp_path: Path):
    values = {"analysis": {"readonly_code": True}, "tools": {"sql": False}}
    plugin = _plugin(values)
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert result.env["DATAELF_FINANCE_READONLY_CODE"] == "1"


def test_finance_analysis_settings_flow_to_run_env(tmp_path: Path):
    values = {
        "analysis": {
            "http_user_agent": "DataElf Finance (contact: analyst@example.com)",
            "retrieve_information_model": "provider/model",
        },
        "tools": {"edgar": True, "python": False},
    }
    plugin = _plugin(values)
    result = plugin.prepare(plugin.normalize_spec(_spec()), str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert result.env["DATAELF_FINANCE_USER_AGENT"] == values["analysis"]["http_user_agent"]
    assert result.env["DATAELF_FINANCE_RETRIEVE_MODEL"] == "provider/model"


def test_node_env_proxy_setting_preserves_explicit_disable(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NODE_USE_ENV_PROXY", "0")
    values = {"tools": {"edgar": True, "python": False}}
    config = _config(values)
    plugin = create_plugin(config, MANIFEST)
    result = plugin.prepare(plugin.normalize_spec(_spec()), str(tmp_path / "shell"), config)
    assert result.status == "completed", result.error_message
    assert result.env["NODE_USE_ENV_PROXY"] == "0"

    # A root-level env entry is explicit configuration and takes precedence
    # over the inherited process environment.
    monkeypatch.setenv("NODE_USE_ENV_PROXY", "1")
    configured = config.model_copy(update={"env": {"NODE_USE_ENV_PROXY": "0"}})
    configured_plugin = create_plugin(configured, MANIFEST)
    configured_result = configured_plugin.prepare(
        configured_plugin.normalize_spec(_spec()), str(tmp_path / "config"), configured,
    )
    assert configured_result.status == "completed", configured_result.error_message
    assert configured_result.env["NODE_USE_ENV_PROXY"] == "0"


def test_node_env_proxy_defaults_on_when_unset(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("NODE_USE_ENV_PROXY", raising=False)
    values = {"tools": {"edgar": True, "python": False}}
    config = _config(values)
    plugin = create_plugin(config, MANIFEST)
    result = plugin.prepare(plugin.normalize_spec(_spec()), str(tmp_path), config)
    assert result.status == "completed", result.error_message
    assert result.env["NODE_USE_ENV_PROXY"] == "1"


def test_tool_call_budget_flows_to_extension_env(tmp_path: Path):
    values = {"analysis": {"max_tool_calls": 7}, "tools": {"web": True, "sql": False, "python": False}}
    plugin = _plugin(values)
    result = plugin.prepare(plugin.normalize_spec(_spec()), str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert result.env["DATAELF_FINANCE_MAX_TOOL_CALLS"] == "7"

    overridden = plugin.prepare(plugin.normalize_spec(_spec(max_tool_calls=3)), str(tmp_path / "ws2"), _config(values))
    assert overridden.env["DATAELF_FINANCE_MAX_TOOL_CALLS"] == "3"


def test_prepare_web_only_run_has_no_data_sources(tmp_path: Path):
    values = {"benchmark": "finsearchcomp_t2"}
    plugin = _plugin(values)
    spec = plugin.normalize_spec(_spec())
    assert spec.parameters["finance_tool_flags"]["web"] is True
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert result.context["source_kinds"] == []
    assert "DATAELF_FINANCE_DB" not in result.env
    assert (tmp_path / "ws" / "raw" / "finance" / "input").is_dir()
    assert result.env["DATAELF_FINANCE_ALLOWED_TOOLS"].split(",") == ["execute_code", "list_files", "get_field_description"]


def test_benchmark_needs_missing_source_fails_preflight(tmp_path: Path):
    # dataclawbench needs files but none are configured.
    plugin = _plugin({"benchmark": "dataclawbench"})
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path), _config({"benchmark": "dataclawbench"}))
    assert result.status == "failed"
    assert result.error_code == "FINANCE_SOURCE_MISSING"
    assert "files" in result.error_message


def test_sql_tools_without_database_fails_preflight(tmp_path: Path):
    plugin = _plugin({"tools": {"web": True, "sql": False, "python": False}})
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path), _config({"tools": {"web": True, "sql": False, "python": False}}))
    assert result.status == "completed", result.error_message

    plugin = _plugin({"tools": {"sql": True, "python": False}})
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path), _config({"tools": {"sql": True, "python": False}}))
    assert result.status == "failed"
    assert result.error_code == "FINANCE_DB_MISSING"


def test_terminal_flag_is_gone_from_every_surface():
    # Pi's built-in terminal tools are always present; the flag no longer
    # exists in any layer and must be rejected everywhere it used to live.
    plugin = _plugin()
    with pytest.raises(ValueError, match="Unknown finance_tools flags: terminal"):
        plugin.normalize_spec(_spec(finance_tools={"terminal": True}))
    with pytest.raises(ValueError):
        FinanceDomainConfig.from_mapping({"tools": {"terminal": True}})
    from dataelf.domains.finance.config import _tools_from_env

    with pytest.raises(ValueError, match="Unknown Finance tool flags"):
        _tools_from_env("terminal")


def test_all_flags_off_still_runs_on_pi_terminal_tools(tmp_path: Path):
    # No domain tool group enabled: the run stays viable on Pi's built-in
    # terminal tools (read/bash/edit/write/grep/find/ls), so preflight passes.
    plugin = _plugin()
    spec = plugin.normalize_spec(_spec(finance_tools={"sql": False, "python": False, "web": False, "edgar": False, "prices": False}))
    assert spec.parameters["finance_tool_names"] == ["read", "bash", "edit", "write", "grep", "find", "ls"]
    result = plugin.prepare(spec, str(tmp_path), _config())
    assert result.status == "completed", result.error_message
    assert result.env["DATAELF_FINANCE_ALLOWED_TOOLS"] == ""


def test_excel_input_without_extra_fails_preflight(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import plugin as finance_plugin

    data = tmp_path / "source" / "report.xlsx"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"not a real workbook")
    values = {"source": {"files": {"data_path": str(data)}}, "tools": {"sql": False}}
    plugin = _plugin(values)
    monkeypatch.setattr(finance_plugin, "_probe_import", lambda module: False)
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "failed"
    assert result.error_code == "FINANCE_FILE_DEPS_MISSING"
    assert "openpyxl" in result.error_message


def test_excel_and_pdf_input_with_extra_passes_preflight(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import plugin as finance_plugin

    source = tmp_path / "source"
    source.mkdir()
    (source / "report.xlsx").write_bytes(b"not a real workbook")
    (source / "brief.pdf").write_bytes(b"not a real pdf")
    values = {"source": {"files": {"data_path": str(source)}}, "tools": {"sql": False}}
    plugin = _plugin(values)
    probed: list[str] = []
    monkeypatch.setattr(finance_plugin, "_probe_import", lambda module: probed.append(module) or True)
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message
    assert probed == ["openpyxl", "pypdf"]


def test_stdlib_analyzable_input_skips_file_dep_probe(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import plugin as finance_plugin

    data = tmp_path / "source" / "input.csv"
    data.parent.mkdir(parents=True)
    data.write_text("ticker,close\nAAPL,180.5\n", encoding="utf-8")
    values = {"source": {"files": {"data_path": str(data)}}, "tools": {"sql": False}}
    plugin = _plugin(values)
    monkeypatch.setattr(finance_plugin, "_probe_import", lambda module: pytest.fail("probe must not run for stdlib-analyzable inputs"))
    spec = plugin.normalize_spec(_spec())
    result = plugin.prepare(spec, str(tmp_path / "ws"), _config(values))
    assert result.status == "completed", result.error_message


def test_agent_resources_isolated_per_benchmark():
    plugin = _plugin()
    generic = plugin.agent_resources(_spec(), _config())
    assert [path.name for path in generic.extensions] == ["finance_sql_python.ts"]

    # finance_agent_bench_v1_1 declares no extension yet (skeleton registers
    # nothing); its edgar flag adds the generic market-data extension.
    selected = plugin.agent_resources(_spec(benchmark="finance_agent_bench_v1_1"), _config())
    assert [path.name for path in selected.extensions] == ["finance_sql_python.ts", "finance_edgar_prices.ts"]

    resolved = get_benchmark("finance_agent_bench_v1_1")
    extensions, skills = [], []
    from dataelf.domains.finance.benchmarks import benchmark_pi_resources

    extensions, skills = benchmark_pi_resources(resolved)
    assert extensions == [] and skills == []


def test_benchmark_pi_resources_validated(tmp_path: Path):
    from dataelf.domains.finance.benchmarks import ResolvedBenchmark, benchmark_pi_resources
    from dataelf.domains.finance.config import BenchmarkConfig

    extension = tmp_path / "pi" / "extensions" / "tool.ts"
    extension.parent.mkdir(parents=True)
    extension.write_text("export default function () {}\n", encoding="utf-8")
    skill = tmp_path / "pi" / "skills" / "bench" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# bench\n", encoding="utf-8")
    config = BenchmarkConfig.model_validate({
        "benchmark": "probe", "display_name": "Probe",
        "prompt": {"source_guidance": "s", "tool_guidance": "t", "output_guidance": "o", "stopping_policy": "p"},
        "pi": {"extensions": ["pi/extensions/tool.ts"], "skills": ["pi/skills/bench/SKILL.md"]},
    })
    extensions, skills = benchmark_pi_resources(ResolvedBenchmark(config=config, folder=tmp_path))
    assert extensions == [extension.resolve()]
    assert skills == [skill.resolve()]

    missing = config.model_copy(deep=True)
    missing.pi.extensions = [Path("pi/extensions/absent.ts")]
    with pytest.raises(ValueError, match="does not exist"):
        benchmark_pi_resources(ResolvedBenchmark(config=missing, folder=tmp_path))

    escaping = config.model_copy(deep=True)
    escaping.pi.extensions = ["../../outside.ts"]
    with pytest.raises(ValueError, match="escapes"):
        benchmark_pi_resources(ResolvedBenchmark(config=escaping, folder=tmp_path))


def test_benchmark_extra_tools_need_extension_or_known_name():
    from dataelf.domains.finance.config import BenchmarkConfig

    base = {
        "benchmark": "probe", "display_name": "Probe",
        "prompt": {"source_guidance": "s", "tool_guidance": "t", "output_guidance": "o", "stopping_policy": "p"},
        "tools": {"extra": ["mystery_tool"]},
    }
    # A name nothing registers would be advertised in the prompt for nothing.
    with pytest.raises(ValueError, match="mystery_tool"):
        BenchmarkConfig.model_validate(base)

    # Web tools ship with the common Pi layer; no benchmark extension needed.
    known = BenchmarkConfig.model_validate({**base, "tools": {"extra": ["web_search"]}})
    assert known.tools.extra == ["web_search"]

    # A benchmark-local extension may register arbitrary names.
    with_ext = BenchmarkConfig.model_validate({**base, "pi": {"extensions": ["pi/extensions/tool.ts"]}})
    assert with_ext.tools.extra == ["mystery_tool"]


def test_prompt_lists_web_tools_for_web_benchmark():
    plugin = _plugin({"benchmark": "finsearchcomp_t2"})
    spec = plugin.normalize_spec(_spec("Compare AAPL and MSFT margins"))
    job = DiscoveryJob.model_validate({
        "job_id": "job_test", "spec": spec.model_dump(), "status": "running",
        "workspace_path": "/tmp/ws", "artifacts": [],
    })
    from dataelf.discovery.contracts import DiscoveryContext

    context = DiscoveryContext(workspace_path="/tmp/ws", spec=spec, manifest=MANIFEST)
    prompt = plugin.build_prompt(job, context)
    assert "web_search" in prompt and "fetch_content" in prompt
    assert "results/results.json" in prompt and "results/final_brief.md" in prompt
    assert "```json" in prompt and '"summary"' in prompt and '"minimum": 0' in prompt
    # Benchmark identity is orchestration metadata and must not reach the
    # agent; only the profile's guidance content travels.
    assert "FinSearchComp Track 2" not in prompt


def test_review_summary_contract(tmp_path: Path):
    plugin = _plugin()
    spec = plugin.normalize_spec(_spec())
    job = DiscoveryJob.model_validate({
        "job_id": "job_review", "spec": spec.model_dump(), "status": "running",
        "workspace_path": str(tmp_path), "artifacts": [],
    })
    results = tmp_path / "results"
    results.mkdir(parents=True)
    (results / "results.json").write_text(json.dumps({
        "summary": "Margins expanded on mix shift",
        "key_findings": ["f1"], "evidence_refs": ["q1"], "confidence": 0.8, "limitations": "l",
    }), encoding="utf-8")
    review = plugin.review(job, str(tmp_path))
    assert review.status == "pass"
    assert review.metrics == {"summary_present": True}
    assert plugin.result_ids(str(tmp_path)) == ["finance_results"]

    # Loose shapes the model naturally produces must pass without warnings:
    # findings as objects with per-finding evidence, limitations as a list,
    # plus undeclared extra fields.
    (results / "results.json").write_text(json.dumps({
        "summary": "Rare earth prices surged",
        "key_findings": [
            {"finding": "f1", "supporting_data": {"MP": {"return_pct": 145.5}},
             "evidence_refs": ["raw/finance/prices.json"], "confidence": 0.97},
            "plain string finding",
        ],
        "evidence_refs": ["raw/finance/prices.json"], "confidence": 0.9,
        "limitations": ["proxy is equity prices", "weekly frequency"],
        "methodology": "price_history weekly closes, no fx adjustment",
    }), encoding="utf-8")
    review = plugin.review(job, str(tmp_path))
    assert review.status == "pass" and review.warnings == []

    (results / "results.json").write_text(json.dumps({
        "summary": "s", "key_findings": ["f"], "confidence": 5,
    }), encoding="utf-8")
    review = plugin.review(job, str(tmp_path))
    assert review.status == "pass_with_warnings"
    assert review.warnings

    (results / "results.json").unlink()
    review = plugin.review(job, str(tmp_path))
    assert review.status == "failed"
    assert plugin.result_ids(str(tmp_path)) == []


def test_runtime_field_overrides_flow_to_review(tmp_path: Path):
    plugin = _plugin()
    spec = plugin.normalize_spec(_spec(finance_output_fields=[
        {"name": "verdict"}, {"name": "limitations", "required": False},
    ]))
    assert spec.parameters["finance_output_fields"] == [
        {"name": "verdict"}, {"name": "limitations", "required": False},
    ]
    job = DiscoveryJob.model_validate({
        "job_id": "job_override", "spec": spec.model_dump(), "status": "running",
        "workspace_path": str(tmp_path), "artifacts": [],
    })
    results = tmp_path / "results"
    results.mkdir(parents=True)
    payload = {
        "summary": "s", "key_findings": ["f"],
        "evidence_refs": ["q"], "confidence": 0.7, "verdict": "buy",
    }
    (results / "results.json").write_text(json.dumps(payload), encoding="utf-8")
    review = plugin.review(job, str(tmp_path))
    assert review.status == "pass"

    payload.pop("verdict")
    (results / "results.json").write_text(json.dumps(payload), encoding="utf-8")
    review = plugin.review(job, str(tmp_path))
    assert review.status == "pass_with_warnings"
    assert any("verdict" in warning for warning in review.warnings)


def test_runtime_field_overrides_fail_fast_on_bad_kind():
    # The old bug: KeyError deep in prompt rendering, after prepare had run.
    plugin = _plugin()
    with pytest.raises(ValueError, match="must be one of"):
        plugin.normalize_spec(_spec(finance_output_fields=[{"name": "v", "kind": "boolean"}]))


def test_runtime_field_overrides_fail_fast_on_non_bool_required():
    # bool("false") used to silently become True.
    plugin = _plugin()
    with pytest.raises(ValueError, match="must be true or false"):
        plugin.normalize_spec(_spec(finance_output_fields=[{"name": "v", "required": "false"}]))


def test_runtime_field_overrides_valid_kind_renders_prompt():
    plugin = _plugin()
    spec = plugin.normalize_spec(_spec(finance_output_fields=[{"name": "score", "kind": "number"}]))
    job = DiscoveryJob.model_validate({
        "job_id": "job_kind", "spec": spec.model_dump(), "status": "running",
        "workspace_path": "/tmp/ws", "artifacts": [],
    })
    from dataelf.discovery.contracts import DiscoveryContext

    prompt = plugin.build_prompt(job, DiscoveryContext(workspace_path="/tmp/ws", spec=spec, manifest=MANIFEST))
    assert '"score"' in prompt and '"type": "number"' in prompt


FILING_HTML = (
    "<html><head><title>Apple 10-K</title><style>.x{}</style></head>"
    "<body><p>Net sales were <b>394,328</b> million.</p>"
    "<table><tr><th>Year</th><th>Revenue</th></tr><tr><td>2023</td><td>394,328</td></tr></table>"
    "</body></html>"
)


def _finance_code_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw" / "finance" / "input"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_parse_html_page_parses_local_filing(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    (root / "filing.htm").write_text(FILING_HTML, encoding="utf-8")
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))

    result = tools.dispatch("parse_html_page", {"path": "filing.htm"})
    assert result["title"] == "Apple 10-K"
    assert "Net sales were 394,328 million." in result["text"]
    assert result["tables"] == [[["Year", "Revenue"], ["2023", "394,328"]]]
    assert result["table_count"] == 1

    tables_only = tools.dispatch("parse_html_page", {"path": "filing.htm", "extract": "tables"})
    assert "text" not in tables_only
    assert tables_only["tables"]

    monkeypatch.setenv("DATAELF_WORKSPACE", str(tmp_path))
    parsed = tools.dispatch("parse_html_page", {"path": "filing.htm", "key": "filing"})
    stored = (tmp_path / "raw" / "finance" / "storage" / "filing.txt").read_text(encoding="utf-8")
    assert "2023 | 394,328" in stored
    assert parsed["text_length"] == len(stored)


def test_parse_html_page_url_branch_reads_cached_page(tmp_path: Path, monkeypatch):
    import hashlib

    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    url = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    pages = root.parent / "pages"
    pages.mkdir(parents=True)
    (pages / f"{digest}.html").write_text(FILING_HTML, encoding="utf-8")

    # Cache hit: no network access in this test.
    result = tools.dispatch("parse_html_page", {"url": url})
    assert result["path"] == f"pages/{digest}.html"
    assert result["url"] == url
    assert result["title"] == "Apple 10-K"


def test_parse_html_page_rejects_invalid_arguments(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    (root / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))

    for args, message in [
        ({}, "exactly one"),
        ({"url": "https://x/y", "path": "z"}, "exactly one"),
        ({"path": "../secrets.html"}, "workspace-relative"),
        ({"url": "http://x/y"}, "https://"),
        ({"path": "notes.txt"}, "expects an"),
    ]:
        with pytest.raises(ValueError, match=message):
            tools.dispatch("parse_html_page", args)


def _big_filing_html(paragraphs: int = 900, tables: int = 12) -> str:
    body = ["<html><head><title>Big filing</title></head><body>"]
    body.extend(f"<p>Paragraph {index} with repeated filing narrative text. </p>" for index in range(paragraphs))
    for table in range(tables):
        body.append("<table>")
        body.extend(
            "<tr>" + "".join(f"<td>cell {table}-{row}-{column}</td>" for column in range(35)) + "</tr>"
            for row in range(60)
        )
        body.append("</table>")
    body.append("</body></html>")
    return "".join(body)


def test_parse_html_page_bounds_text_tables_and_keeps_full_storage(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    monkeypatch.setenv("DATAELF_WORKSPACE", str(tmp_path))
    (root / "big.htm").write_text(_big_filing_html(), encoding="utf-8")

    result = tools.dispatch("parse_html_page", {"path": "big.htm", "key": "big", "extract": "all"})
    # Default max_length is 12000 and the hard cap is 30000.
    assert result["returned_text_length"] == 12000
    assert result["result_limit"] == {"unit": "characters", "limit": 12000}
    assert result["text"].endswith("...[truncated]")
    assert result["truncated"] is True
    # Tables: 12 parsed, 10 returned, 50 rows x 30 cells each.
    assert result["table_count"] == 12
    assert len(result["tables"]) == 10
    assert all(len(table) == 50 and all(len(row) == 30 for row in table) for table in result["tables"])
    assert result["table_truncated"] is True
    assert result["table_limits"]["tables"] == 10

    # max_length above the hard cap is clamped, not honored.
    clamped = tools.dispatch("parse_html_page", {"path": "big.htm", "max_length": 30001})
    assert clamped["returned_text_length"] == 30000
    assert clamped["result_limit"]["limit"] == 30000

    # extract=text returns no tables payload at all.
    text_only = tools.dispatch("parse_html_page", {"path": "big.htm", "extract": "text", "max_length": 500})
    assert "tables" not in text_only and "table_truncated" not in text_only
    assert text_only["returned_text_length"] == 500

    # Storage keeps the FULL text for later range reads.
    stored = tmp_path / "raw" / "finance" / "storage" / "big.txt"
    full_text = stored.read_text(encoding="utf-8")
    assert len(full_text) == result["text_length"] > 30000
    assert "Paragraph 899" in full_text


def test_retrieve_information_enforces_key_prompt_and_range_limits(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    monkeypatch.setenv("DATAELF_WORKSPACE", str(tmp_path))
    storage = tmp_path / "raw" / "finance" / "storage"
    storage.mkdir(parents=True)
    (storage / "large.txt").write_text("L" * 40_000, encoding="utf-8")
    (storage / "small.txt").write_text("S" * 100, encoding="utf-8")

    # Above the per-key cap without ranges: actionable error.
    with pytest.raises(ValueError, match="input_character_ranges"):
        tools.dispatch("retrieve_information", {"prompt": "Summarize {{large}}"})

    # A bounded range succeeds and reports metadata.
    ok = tools.dispatch("retrieve_information", {
        "prompt": "Summarize {{large}}",
        "input_character_ranges": [{"key": "large", "start": 0, "end": 12000}],
    })
    assert ok["prompt_length"] == len("Summarize ") + 12000
    assert ok["character_ranges"] == {"large": [0, 12000]}
    assert ok["stored_lengths"] == {"large": 40_000}
    assert ok["truncated"] is False

    # Oversized ranges are rejected with the limit in the message.
    with pytest.raises(ValueError, match="16000"):
        tools.dispatch("retrieve_information", {
            "prompt": "Summarize {{large}}",
            "input_character_ranges": [{"key": "large", "start": 0, "end": 20000}],
        })

    # Ranges past the document end are clipped and reported.
    clipped = tools.dispatch("retrieve_information", {
        "prompt": "Summarize {{small}}",
        "input_character_ranges": [{"key": "small", "start": 50, "end": 500}],
    })
    assert clipped["character_ranges"] == {"small": [50, 100]}
    assert clipped["truncated"] is True

    # Combined expansions above the total prompt cap fail with guidance.
    with pytest.raises(ValueError, match="30000"):
        tools.dispatch("retrieve_information", {
            "prompt": "Compare {{large}} with {{large}} again",
            "input_character_ranges": [{"key": "large", "start": 0, "end": 16000}],
        })


def test_parse_and_retrieve_chain_uses_ranges_on_full_text(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    monkeypatch.setenv("DATAELF_WORKSPACE", str(tmp_path))
    (root / "filing.htm").write_text(_big_filing_html(paragraphs=900, tables=1), encoding="utf-8")

    parsed = tools.dispatch("parse_html_page", {"path": "filing.htm", "key": "msft10k", "max_length": 12000})
    assert parsed["truncated"] is True and parsed["returned_text_length"] == 12000

    retrieved = tools.dispatch("retrieve_information", {
        "prompt": "From {{msft10k}} extract the fiscal year.",
        "input_character_ranges": [{"key": "msft10k", "start": 12000, "end": 20000}],
    })
    assert retrieved["prompt_length"] == len("From ") + 8000 + len(" extract the fiscal year.")
    assert retrieved["stored_lengths"]["msft10k"] == parsed["text_length"]
    full_text = (tmp_path / "raw" / "finance" / "storage" / "msft10k.txt").read_text(encoding="utf-8")
    assert retrieved["prompt"][5:25] == full_text[12000:12020]


def test_list_files_limit_and_truncation(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    for index in range(550):
        (root / f"f{index:04d}.csv").write_text("x\n", encoding="utf-8")

    default = tools.dispatch("list_files", {})
    assert default["returned"] == 100 and len(default["files"]) == 100
    assert default["truncated"] is True and default["total_matches"] == 550
    assert default["result_limit"] == {"unit": "items", "limit": 100}

    capped = tools.dispatch("list_files", {"limit": 501})
    assert capped["returned"] == 500 and capped["result_limit"]["limit"] == 500
    assert capped["truncated"] is True

    explicit = tools.dispatch("list_files", {"limit": 200})
    assert explicit["returned"] == 200 and explicit["truncated"] is True


def test_execute_code_reports_output_truncation(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))

    big = tools.dispatch("execute_code", {"code": "print('o' * 25000)\nimport sys; sys.stderr.write('e' * 15000)"})
    assert big["stdout_truncated"] is True and len(big["stdout"]) == 20000
    assert big["stderr_truncated"] is True and len(big["stderr"]) == 12000
    assert big["returncode"] == 0

    small = tools.dispatch("execute_code", {"code": "print('ok')"})
    assert small["stdout_truncated"] is False and small["stderr_truncated"] is False
    assert small["stdout"] == "ok\n"


def test_execute_code_readonly_blocks_writes_to_linked_sources(tmp_path: Path, monkeypatch):
    import os
    import sqlite3

    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    db = corpus / "finance.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    data = corpus / "prices.csv"
    data.write_text("ticker,close\nAAPL,180.5\n", encoding="utf-8")
    monkeypatch.setenv("DATAELF_FINANCE_PROTECTED", os.pathsep.join([str(db), str(corpus)]))

    # reads through a plain connect are rewritten to a read-only URI
    reads = tools.dispatch("execute_code", {"code": f"import sqlite3; print(sqlite3.connect({str(db)!r}).execute('SELECT COUNT(*) FROM t').fetchone()[0])"})
    assert reads["returncode"] == 0
    assert reads["stdout"].strip() == "1"

    # sqlite writes fail with "attempt to write a readonly database"
    sqlite_write = tools.dispatch("execute_code", {"code": f"import sqlite3; conn = sqlite3.connect({str(db)!r}); conn.execute('CREATE TABLE evil (a)'); conn.commit()"})
    assert sqlite_write["returncode"] == 1
    assert "readonly" in sqlite_write["stderr"]
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='evil'").fetchone()[0] == 0

    # plain file writes to the corpus are refused
    file_write = tools.dispatch("execute_code", {"code": f"open({str(data)!r}, 'w').write('clobbered')"})
    assert file_write["returncode"] == 1
    assert "read-only enforcement" in file_write["stderr"]
    assert data.read_text(encoding="utf-8") == "ticker,close\nAAPL,180.5\n"

    # spawning processes is refused (a child would not carry the audit hook)
    escaped = tools.dispatch("execute_code", {"code": "import subprocess; subprocess.run(['echo', 'hi'])"})
    assert escaped["returncode"] == 1
    assert "subprocess.Popen" in escaped["stderr"]

    # workspace scratch writes outside the protected roots stay allowed
    scratch = tools.dispatch("execute_code", {"code": "open('scratch.txt', 'w').write('ok'); print(open('scratch.txt').read())"})
    assert scratch["returncode"] == 0
    assert scratch["stdout"].strip() == "ok"


def test_execute_code_readonly_can_be_forced_off(tmp_path: Path, monkeypatch):
    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    data = tmp_path / "corpus" / "prices.csv"
    data.parent.mkdir()
    data.write_text("ticker,close\nAAPL,180.5\n", encoding="utf-8")
    monkeypatch.setenv("DATAELF_FINANCE_PROTECTED", str(data.parent))
    monkeypatch.setenv("DATAELF_FINANCE_READONLY_CODE", "0")

    result = tools.dispatch("execute_code", {"code": f"open({str(data)!r}, 'a').write('appended')"})
    assert result["returncode"] == 0
    assert data.read_text(encoding="utf-8").endswith("appended")


def test_execute_query_limit_and_cell_truncation(tmp_path: Path, monkeypatch):
    import sqlite3

    from dataelf.domains.finance import tools

    root = _finance_code_root(tmp_path)
    monkeypatch.setenv("DATAELF_FINANCE_CODE_ROOT", str(root))
    database = tmp_path / "finance.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE notes (id INTEGER, body TEXT)")
        conn.executemany(
            "INSERT INTO notes VALUES (?, ?)",
            [(index, "n" * 2500 if index % 10 == 0 else f"note {index}") for index in range(150)],
        )
        conn.commit()
    monkeypatch.setenv("DATAELF_FINANCE_DB", str(database))

    capped = tools.dispatch("execute_query", {"query": "SELECT * FROM notes", "limit": 100})
    assert capped["returned"] == 100 and capped["truncated"] is True
    assert capped["result_limit"] == {"unit": "rows", "limit": 100}
    assert all(len(row["body"]) <= 2000 for row in capped["rows"])
    assert capped["truncated_cells"] == 10

    default = tools.dispatch("execute_query", {"query": "SELECT * FROM notes"})
    assert default["returned"] == 20 and default["truncated"] is True
    assert default["result_limit"]["limit"] == 20


def test_sqlite_read_only_guard_ignores_literals_and_allows_quoted_punctuation():
    _assert_read_only("SELECT 'attach ' AS note")
    _assert_read_only("SELECT replace(name, 'a', 'b') FROM companies")
    assert _safe_identifier("price-history") == "price-history"
    assert _safe_identifier("2024.results") == "2024.results"
    with pytest.raises(ValueError, match="read-only"):
        _assert_read_only("ATTACH DATABASE 'other.db' AS other")


def test_finance_run_completes_with_fake_pi(tmp_path: Path):
    from dataelf.config import ExplorerConfig, PiConfig, RuntimeConfig
    from dataelf.discovery.workflow import run_job

    pi = tmp_path / "fake_pi"
    pi.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '{"type":"session","version":3,"id":"fake-pi","cwd":"%s"}\n' "$PWD"
cd "$DATAELF_WORKSPACE"
mkdir -p results scripts
cat > results/results.json <<'JSON'
{"summary":"Revenue concentration: revenue declined year over year in the sample data.","key_findings":["Revenue declined across all sample periods."],"evidence_refs":["query:financial_facts"],"confidence":0.9,"limitations":"Sample data only."}
JSON
printf '# Finance brief\n\nSample-backed synthesis.\n' > results/final_brief.md
""",
        encoding="utf-8",
    )
    pi.chmod(0o755)
    import sqlite3

    database = tmp_path / "finance.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, cik TEXT, fact_name TEXT,"
            " fact_value REAL, unit TEXT, fiscal_year INTEGER, fiscal_period TEXT)"
        )
        conn.executemany(
            "INSERT INTO financial_facts (cik, fact_name, fact_value, unit, fiscal_year, fiscal_period)"
            " VALUES ('6201', 'Revenues', ?, 'USD', ?, 'FY')",
            [(1000.0, 2023), (900.0, 2022)],
        )
        conn.commit()
    config = DataElfConfig(
        runtime=RuntimeConfig(
            workspace_dir=tmp_path / ".dataelf",
            sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
            workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        ),
        explorer=ExplorerConfig(pi=PiConfig(binary=str(pi), model="openai/gpt-test")),
        domains={"finance": {
            "benchmark": "ddr_10k",
            "source": {"sqlite": {"db_path": str(database)}},
        }},
    )
    job = run_job(JobSpec(domain="finance", objective="Analyze company with CIK 6201"), config)
    assert job.status == "completed", job.error_message
    workspace = Path(job.workspace_path)
    results = json.loads((workspace / "results" / "results.json").read_text(encoding="utf-8"))
    assert "result_id" not in results
    assert results["summary"].startswith("Revenue concentration")
    review = json.loads((workspace / "reviews" / "quality_review.json").read_text(encoding="utf-8"))
    assert review["status"] == "pass"
    prompt = (workspace / "prompts" / "discovery_prompt.md").read_text(encoding="utf-8")
    # Identity hidden (orchestration metadata), guidance content present.
    assert "DDR-Bench 10-K Filings" not in prompt
    assert "SEC 10-K filing data" in prompt
    assert "get_database_info" in prompt and "web_search" not in prompt
    index = json.loads((workspace / "workspace_index.json").read_text(encoding="utf-8"))
    assert index["result_ids"] == ["finance_results"]
