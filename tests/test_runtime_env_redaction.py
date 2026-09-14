"""Public runtime regression tests: synthetic credentials, no SDK or network."""
import copy
import json
import logging
import os
import sqlite3
import sys

import pytest

from dataelf.discovery.contracts import (
    ArtifactRef, DiscoveryContext, DiscoveryJob, DomainManifest, ExplorerRunResult,
    JobSpec, ModelingStageResult, StageResult,
)
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer, _redact_env
from dataelf.discovery.workflow import NullStore, _trace_stage
from dataelf.stores.sqlite_store import SQLiteStore


SYNTHETIC_ENV = {
    "WT_SDK_DB_URI": "postgresql://synthetic:synthetic-db-password@invalid/db",
    "DATABASE_URL": "postgresql://synthetic:synthetic-url-password@invalid/db",
    "ANALYTICS_DSN": "host=invalid user=synthetic password=synthetic-dsn-password",
    "SERVICE_CONNECTION_STRING": "Server=invalid;Pwd=synthetic-connection-password",
    "https_proxy": "http://synthetic:synthetic-proxy-password@invalid:8080",
    "CUSTOM_ENDPOINT": "https://synthetic:synthetic-endpoint-password@invalid/path",
    "SIGNED_ENDPOINT": "https://invalid/path?X-Amz-Credential=synthetic-query-credential",
    "dsn": "host=invalid password=synthetic-lowercase-password",
    "AWS_ACCESS_KEY_ID": "synthetic-access-key",
    "AWS_SECRET_ACCESS_KEY": "synthetic-secret-key",
    "API_TOKEN": "synthetic-token",
    "SERVICE_PASSWORD": "synthetic-password",
}


@pytest.mark.parametrize("stage,result_type", [
    ("domain_prepare", StageResult), ("domain_modeling", ModelingStageResult),
])
@pytest.mark.parametrize("env", [SYNTHETIC_ENV, {}, {"EMPTY": "", "OPAQUE": "synthetic-opaque", "ZERO": "0"}])
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_stage_records_only_env_presence(tmp_path, caplog, stage, result_type, env, status):
    artifact = ArtifactRef(artifact_id="synthetic", kind="evidence", path="evidence.json",
                           role="evidence", producer_stage=stage)
    (tmp_path / artifact.path).write_text('{"synthetic": true}')
    result = result_type(status=status, env=env, artifacts=[artifact],
                         context={"synthetic": True}, metrics={"rows": 1},
                         error_code="SYNTHETIC_FAILURE" if status == "failed" else None)
    original = copy.deepcopy(result.model_dump(mode="json"))
    original_env = result.env
    job = DiscoveryJob(job_id="synthetic", spec=JobSpec(domain="synthetic", objective="synthetic"),
                       workspace_path=str(tmp_path))
    database = tmp_path / "trace.sqlite"
    store = SQLiteStore(database)
    store.init_schema()
    try:
        _trace_stage(store, job, stage, result)
    finally:
        store.close()
    # A fresh connection verifies committed disk contents after store closure.
    with sqlite3.connect(database) as reopened:
        event_type, raw = reopened.execute("SELECT event_type, payload_json FROM trace_events").fetchone()
    with caplog.at_level(logging.DEBUG, logger="dataelf.discovery"):
        _trace_stage(NullStore(), job, stage, result)
    expected = {**original, "env": {key: bool(value) for key, value in env.items()}}
    assert json.loads(raw) == expected
    assert event_type == stage + "_completed"
    assert caplog.records[-1].args[2] == expected
    assert all(value not in raw + caplog.text for value in env.values() if value and value != "0")
    assert result.model_dump(mode="json") == original
    assert result.env is original_env


def test_result_without_env_does_not_gain_env(tmp_path, caplog):
    result = ExplorerRunResult(status="completed")
    original = result.model_dump(mode="json")
    job = DiscoveryJob(job_id="synthetic", spec=JobSpec(domain="synthetic", objective="synthetic"),
                       workspace_path=str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="dataelf.discovery"):
        _trace_stage(NullStore(), job, "explorer", result)
    assert caplog.records[-1].args[2] == original
    assert "env" not in caplog.records[-1].args[2]
    assert result.model_dump(mode="json") == original


def test_actual_pi_diagnostic_and_child_env(tmp_path, monkeypatch):
    # Only this test process's environment is isolated; production inheritance is untouched.
    for key in list(os.environ):
        monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    values = {
        **SYNTHETIC_ENV, "EMPTY_TOKEN": "", "EMPTY_DSN": "",
        "DATA_PATH": str(tmp_path / "data"), "PI_TELEMETRY": "0",
        "PUBLIC_ENDPOINT": "https://public.invalid/path", "NO_PROXY": "localhost,127.0.0.1",
        "PI_CODING_AGENT_DIR": str(tmp_path / "agent"),
        "NPM_CONFIG_CACHE": str(tmp_path / "cache"),
    }
    fake = tmp_path / "synthetic_pi"
    fake.write_text(f"#!{sys.executable} -B\nimport json, os, sys\n"
                    f"expected = {values!r}\n"
                    "ok = all(os.environ.get(k) == v for k, v in expected.items())\n"
                    "print(json.dumps({'type': 'agent_end', 'result': 'PASS' if ok else 'FAIL'}))\n"
                    "sys.exit(0 if ok else 1)\n")
    fake.chmod(0o755)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("synthetic offline environment check")
    spec = JobSpec(domain="synthetic", objective="synthetic")
    job = DiscoveryJob(job_id="synthetic", spec=spec, workspace_path=str(tmp_path))
    context = DiscoveryContext(workspace_path=str(tmp_path), spec=spec,
        manifest=DomainManifest(domain="synthetic", version="1", display_name="Synthetic", plugin="synthetic:create"),
        env=values, prompt_path=str(prompt))
    original = context.model_dump(mode="json")
    result = PiCliInsightsExplorer(pi_binary=str(fake), cwd=tmp_path, model="",
                                   extra_args="", log_mode="quiet").run(job, context)
    assert result.status == "completed"
    assert json.loads((tmp_path / "logs/pi_stdout.log").read_text())["result"] == "PASS"
    raw = (tmp_path / "logs/pi_env_redacted.json").read_text()
    diagnostic = json.loads(raw)
    for key, value in SYNTHETIC_ENV.items():
        assert diagnostic[key] == "<redacted>"
        assert value not in raw
    for key in values.keys() - SYNTHETIC_ENV.keys():
        assert diagnostic[key] == values[key]
    assert diagnostic["HOME"] == str(home)
    assert diagnostic["DATAELF_JOB_ID"] == job.job_id
    assert context.model_dump(mode="json") == original
    # The diagnostic helper must also leave caller mappings intact.
    before = values.copy()
    _redact_env(values)
    assert values == before
