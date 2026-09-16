"""Synthetic bounded acquisition; no WT credentials, network or model inference."""

import json
import subprocess
import sys

import pytest
from test_trajectory_analysis import envelope, fixture_calls, unlocated_report
from test_trajectory_failure_analysis import review_workspace
from test_trajectory_tool_isolation import (
    client_env,
    init_client_workspace,
    synthetic_sdk,
)

from dataelf.domains.trajectory_analysis.analysis import ANALYSIS, evidence_scope
from dataelf.domains.trajectory_analysis.client import TrajectoryClient
from dataelf.domains.trajectory_analysis.connector import (
    FIELDS,
    METADATA,
    RAW,
    EvidenceError,
    project,
    read_json,
    record_call,
)


def fake_client(tmp_path, monkeypatch, calls=None):
    init_client_workspace(tmp_path)
    pending = list(calls or fixture_calls())
    executed = []

    def run(*args, **kwargs):
        request = json.loads(kwargs["input"])
        executed.append(request)
        response = pending.pop(0)["envelope"]
        return subprocess.CompletedProcess(args, 0, json.dumps(response).encode())

    monkeypatch.setattr(
        "dataelf.domains.trajectory_analysis.client.subprocess.run", run
    )
    return TrajectoryClient(tmp_path, sys.executable), executed, pending


def test_reinstantiation_duplicates_switch_and_budget(tmp_path, monkeypatch):
    client, executed, pending = fake_client(tmp_path, monkeypatch)
    client.search_records()
    client.get_record("MOCK_PRIVATE_ID")
    for field in FIELDS[1:]:
        pending.append(
            {
                "envelope": envelope(
                    [{"id": "MOCK_PRIVATE_ID", "reward": 0, field: {"value": field}}]
                )
            }
        )
        client = TrajectoryClient(tmp_path, sys.executable)
        client.get_record("MOCK_PRIVATE_ID", fields=[field])
    assert len(executed) == 6
    assert evidence_scope(read_json(tmp_path, RAW)["calls"]).get_count == 5
    with pytest.raises(EvidenceError, match="QUERY_CALL_COUNT"):
        TrajectoryClient(tmp_path, sys.executable).get_record(
            "MOCK_PRIVATE_ID", fields=["messages"]
        )
    assert len(executed) == 6


@pytest.mark.parametrize("kind", ["duplicate", "switch", "search", "first_field"])
def test_rejected_before_transport(tmp_path, monkeypatch, kind):
    client, executed, _ = fake_client(tmp_path, monkeypatch)
    client.search_records()
    if kind != "first_field":
        client.get_record("MOCK_PRIVATE_ID")
    before = len(executed)
    with pytest.raises(EvidenceError):
        if kind == "search":
            client.search_records()
        else:
            client.get_record(
                "WRONG" if kind == "switch" else "MOCK_PRIVATE_ID",
                fields=["messages"]
                if kind in {"switch", "first_field"}
                else ["chosen_trace"],
            )
    assert len(executed) == before


@pytest.mark.parametrize(
    "state", ["missing", "null", "empty", "omitted", "error", "empty_records"]
)
def test_supplement_states_and_stop(tmp_path, monkeypatch, state):
    client, executed, pending = fake_client(tmp_path, monkeypatch)
    client.search_records()
    client.get_record("MOCK_PRIVATE_ID")
    row = {"id": "MOCK_PRIVATE_ID", "reward": 0}
    if state == "null":
        row["messages"] = None
    if state == "empty":
        row["messages"] = []
    env = envelope([row])
    if state == "omitted":
        env["result"].update(
            truncated=True, omitted_fields=[{"record_index": 0, "field": "messages"}]
        )
    if state == "error":
        env = {"isError": True, "result": {"error": "WT_READ_FAILED"}}
    if state == "empty_records":
        env = envelope([])
    pending.append({"envelope": env})
    client.get_record("MOCK_PRIVATE_ID", fields=["messages"])
    raw = read_json(tmp_path, RAW)["calls"]
    scope = evidence_scope(raw)
    assert scope.call_count == 3 and scope.trace_length == 2
    assert (
        read_json(tmp_path, METADATA)["calls"][1]["chosen_trace"]["state"] == "present"
    )
    if state not in {"error", "empty_records"}:
        assert project(raw[2])["fields"]["messages"]["state"] == state
    else:
        with pytest.raises(EvidenceError, match="QUERY_ACQUISITION_STOPPED"):
            TrajectoryClient(tmp_path, sys.executable).get_record(
                "MOCK_PRIVATE_ID", fields=["response"]
            )
        assert len(executed) == 3
    assert scope.truncated == (state == "omitted")
    report = unlocated_report(raw, "synthetic")
    assert report["status"] == (
        "read_failed" if state == "error" else "insufficient_evidence"
    )


@pytest.mark.parametrize("state", ["empty_records", "missing", "null", "empty", "omitted"])
def test_supplement_review_status_after_valid_trace(tmp_path, state):
    plugin, job, ws, report = review_workspace(tmp_path)
    assert report["status"] == "located"
    assert plugin.review(job, str(ws)).status == "pass"
    first_get = read_json(ws, RAW)["calls"][1]
    row = {key: value for key, value in first_get["envelope"]["result"]["records"][0].items()
           if key != "chosen_trace"}
    if state == "null":
        row["messages"] = None
    elif state == "empty":
        row["messages"] = []
    response = envelope([] if state == "empty_records" else [row])
    if state == "omitted":
        response["result"].update(
            truncated=True, omitted_fields=[{"record_index": 0, "field": "messages"}])
    record_call(ws, "wt_get_record", {**first_get["arguments"], "fields": ["messages"]}, response)
    calls = read_json(ws, RAW)["calls"]
    supplemental = project(calls[-1])
    if state == "empty_records":
        assert supplemental["count"] == 0 and supplemental["fields"] == {}
    else:
        assert supplemental["count"] == 1
        assert supplemental["fields"]["messages"]["state"] == state
    scope = evidence_scope(calls).model_dump()
    assert scope["trace_state"] == "present" and scope["trace_length"] > 0

    insufficient = unlocated_report(calls, job.spec.objective)
    assert insufficient["status"] == "insufficient_evidence"
    (ws / ANALYSIS).write_text(json.dumps(insufficient))
    accepted = plugin.review(job, str(ws))
    assert accepted.status == "pass_with_warnings"
    assert accepted.warnings == ["ANALYSIS_INSUFFICIENT_EVIDENCE"]

    # Retain the previously accepted claims/references; only acquisition scope changes.
    report["scope"] = scope
    if state == "omitted":
        report["limitations"].append("truncated_output")
    (ws / ANALYSIS).write_text(json.dumps(report))
    reviewed = plugin.review(job, str(ws))
    if state in {"empty_records", "omitted"}:
        assert reviewed.status == "failed"
        assert not reviewed.metrics["localization_reported"]
        if state == "empty_records":
            assert reviewed.warnings == ["ANALYSIS_STATUS_MISMATCH"]
    else:
        assert reviewed.status == "pass"


def test_transport_exception_is_durable_and_stops(tmp_path, monkeypatch):
    client, _, _ = fake_client(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic", 60)

    monkeypatch.setattr(
        "dataelf.domains.trajectory_analysis.client.subprocess.run", fail
    )
    assert client.search_records()["isError"]
    assert read_json(tmp_path, RAW)["calls"][0]["transport"] == "error"
    with pytest.raises(EvidenceError):
        TrajectoryClient(tmp_path, sys.executable).search_records()
    assert len(read_json(tmp_path, RAW)["calls"]) == 1


def test_locator_mismatch_retains_actual_response_and_latches(tmp_path, monkeypatch):
    calls = fixture_calls()
    calls[1]["envelope"]["result"]["records"][0]["id"] = "SYNTHETIC_WRONG"
    client, executed, _ = fake_client(tmp_path, monkeypatch, calls)
    client.search_records()
    with pytest.raises(EvidenceError, match="QUERY_CAPTURE_FAILED"):
        client.get_record("MOCK_PRIVATE_ID")
    raw = read_json(tmp_path, RAW)
    assert raw["capture_failed"] and raw["calls"][1]["envelope"] == calls[1]["envelope"]
    with pytest.raises(EvidenceError):
        client.get_record("MOCK_PRIVATE_ID", fields=["messages"])
    assert len(executed) == 2


def test_actual_client_bridge_sdk_restart_and_job_constraint(tmp_path):
    init_client_workspace(tmp_path)
    row = {
        "id": "SYNTHETIC_ID",
        "job_id": "SYNTHETIC_JOB",
        "reward": 0,
        "chosen_trace": [{"action": "synthetic"}],
        "messages": [{"request": "synthetic"}],
    }
    sdk = synthetic_sdk(tmp_path, [row])
    env = client_env(tmp_path, sdk)
    for expr in [
        "c.search_records()",
        "c.get_record('SYNTHETIC_ID')",
        "c.get_record('SYNTHETIC_ID',fields=['messages'])",
    ]:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from dataelf.domains.trajectory_analysis.client import TrajectoryClient; c=TrajectoryClient.from_env(); "
                + expr,
            ],
            env=env,
            cwd=tmp_path,
            capture_output=True,
        )
        assert result.returncode == 0
    calls = read_json(tmp_path, RAW)["calls"]
    assert len(calls) == 3 and calls[2]["arguments"]["job_id"] == "SYNTHETIC_JOB"
    queries = [
        json.loads(x) for x in (tmp_path / "sdk-calls.jsonl").read_text().splitlines()
    ]
    assert "job_id = 'SYNTHETIC_JOB'" in queries[2]["filter_query"]
    assert queries[2]["columns"][-1] == "messages"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from dataelf.domains.trajectory_analysis.client import TrajectoryClient; TrajectoryClient.from_env().get_record('SYNTHETIC_ID',fields=['messages'])",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode != 0 and len(read_json(tmp_path, RAW)["calls"]) == 3


@pytest.mark.parametrize(
    "mutation", ["later_missing", "unrequested", "out_of_bounds", "truncated"]
)
def test_supplement_references_and_truncation(tmp_path, mutation):
    plugin, job, ws, report = review_workspace(tmp_path, "bounded_context")
    if mutation == "later_missing":
        report["task_goal"]["evidence"][0]["pointer"] += "/missing"
    if mutation == "out_of_bounds":
        report["task_goal"]["evidence"][0]["pointer"] = report["task_goal"]["evidence"][
            0
        ]["pointer"].replace("/calls/2/", "/calls/6/")
    if mutation == "unrequested":
        report["task_goal"]["evidence"][0]["pointer"] = report["task_goal"]["evidence"][
            0
        ]["pointer"].replace("/calls/2/", "/calls/1/")
    if mutation == "truncated":
        raw = read_json(ws, RAW)
        raw["calls"][2]["envelope"]["result"]["truncated"] = True
        (ws / RAW).write_text(json.dumps(raw))
        (ws / METADATA).write_text(
            json.dumps({"calls": [project(c) for c in raw["calls"]]})
        )
        report["scope"] = evidence_scope(raw["calls"]).model_dump()
        report["limitations"].append("truncated_output")
    (ws / ANALYSIS).write_text(json.dumps(report))
    assert plugin.review(job, str(ws)).status == "failed"


def test_surface_success_reward_only_stays_insufficient(tmp_path):
    plugin, job, ws, report = review_workspace(tmp_path, "no_ground_truth")
    raw = read_json(ws, RAW)
    raw["calls"][1]["envelope"]["result"]["records"][0]["chosen_trace"] = [
        {"feedback": "Tests passed"},
        {"final": "Agent claims all done"},
    ]
    (ws / RAW).write_text(json.dumps(raw))
    (ws / METADATA).write_text(
        json.dumps({"calls": [project(c) for c in raw["calls"]]})
    )
    report = unlocated_report(raw["calls"], job.spec.objective)
    report["uncertainty"] = (
        "Only passing checks and reward=0 are visible; no task requirement or supported deviation. No supplemental context is present in this synthetic case."
    )
    (ws / ANALYSIS).write_text(json.dumps(report))
    assert plugin.review(job, str(ws)).status == "pass_with_warnings"
    report["status"] = "located"
    (ws / ANALYSIS).write_text(json.dumps(report))
    assert plugin.review(job, str(ws)).status == "failed"


def test_interrupted_attempt_remains_counted(tmp_path, monkeypatch):
    client, _, _ = fake_client(tmp_path, monkeypatch)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "dataelf.domains.trajectory_analysis.client.subprocess.run", interrupt
    )
    with pytest.raises(KeyboardInterrupt):
        client.search_records()
    raw = read_json(tmp_path, RAW)
    assert len(raw["calls"]) == 1 and raw["calls"][0]["transport"] == "error"
    with pytest.raises(EvidenceError):
        TrajectoryClient(tmp_path, sys.executable).search_records()


def test_lock_symlink_rejected(tmp_path, monkeypatch):
    client, executed, _ = fake_client(tmp_path, monkeypatch)
    target = tmp_path / "synthetic-lock"
    target.touch()
    (tmp_path / (RAW + ".lock")).symlink_to(target)
    with pytest.raises(EvidenceError, match="QUERY_LOCK_INVALID"):
        client.search_records()
    assert not executed
