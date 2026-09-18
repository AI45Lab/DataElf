from __future__ import annotations
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread

import pytest

from dataelf.discovery.contracts import JobSpec, StageResult
from dataelf.discovery.pi_cli_explorer import _run_pi_process, _compact_pi_event_line, _model_event_errors
from dataelf.discovery.run_control import RunControl, RunCancelled, activate_run, terminate_process
from dataelf.discovery.workflow import run_job
from dataelf_server.jobs.manager import JobManager
from dataelf_server.jobs.store import JobStore
from dataelf_server.presentation.errors import classify_job_error
from dataelf_server.scope_v2.client import ScopeV2AIIndexClient
from tests.server.helpers import settings
from tests.server.test_manager import wait_terminal, SuccessfulPipeline
from tests.server.test_workflow import components


def test_fifo_started_at_and_shutdown_cannot_complete_late(tmp_path):
    started, release = Event(), Event()
    class Blocked:
        def run(self, **kw):
            started.set()
            release.wait(5)
            return []
        def cancel(self):
            release.set()
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / 'state')
    cfg.server.max_concurrent_jobs = 1
    manager = JobManager(cfg, pipeline=Blocked())
    first = manager.submit({'query':'first'})
    assert started.wait(2)
    second = manager.submit({'query':'second'})
    assert manager.get(first.job_id).started_at
    assert manager.get(second.job_id).started_at is None
    assert manager.get(second.job_id).status == 'queued'
    with pytest.raises(RuntimeError, match='Another'):
        JobManager(cfg)
    manager.close(wait=True)
    reader = JobStore(cfg.database_path)
    for record in (first, second):
        stored = reader.get_job(record.job_id)
        assert stored.status == 'failed' and stored.error.reason == 'service_shutdown'
    reader.close()
    next_manager = JobManager(cfg, pipeline=SuccessfulPipeline())
    next_manager.close(wait=True)


def test_attempt_history_keeps_error_and_workspace(tmp_path):
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / 'state')
    from tests.server.test_manager import FailingPipeline
    manager = JobManager(cfg, pipeline=FailingPipeline())
    try:
        first = manager.submit({'query': '昨天综合总结'})
        wait_terminal(manager, first.job_id)
        (Path(first.workspace_path)/'evidence.txt').write_text('first attempt')
        second = manager.retry(first.job_id)
        wait_terminal(manager, first.job_id)
        history = manager.store.list_attempts(first.job_id)
        assert [r.attempt for r in history] == [1, 2]
        assert all(r.status == 'failed' and r.error for r in history)
        assert history[0].trace_id != history[1].trace_id
        assert second.created_at > first.created_at
        assert Path(first.workspace_path).name == '0001'
        assert Path(second.workspace_path).name == '0002'
        assert (Path(first.workspace_path)/'evidence.txt').read_text() == 'first attempt'
    finally:
        manager.close(wait=True)


@pytest.mark.parametrize('code,category,reason', [
    ('AI_INDEX_MODELING_RAW_EMPTY','source_error','no_data'),
    ('AI_INDEX_MODELING_RAW_ACQUISITION_FAILED','source_error','acquisition_failed'),
    ('AI_INDEX_MODELING_STAGE1_FAILED','analysis_error','ontology_build_failed'),
    ('AI_INDEX_MODELING_RDF_INVALID','analysis_error','ontology_build_failed'),
    ('SERVER_MODELING_FAILED','analysis_error','ontology_build_failed'),
    ('DOMAIN_REVIEW_FAILED','artifact_error','quality_review_failed'),
    ('RUN_CANCELLED','service_error','service_shutdown'),
    ('ai_index_network_error','source_error','connection_failed'),
])
def test_current_core_errors_have_explicit_public_mapping(code,category,reason):
    value = classify_job_error(code, 'failed', 'analyzing_with_pi')
    assert (value.category,value.reason) == (category,reason)


def test_stage_events_and_sqlite_omit_env_and_redact_exception(tmp_path):
    cfg, profile, explorer, calls = components(tmp_path)
    cfg.core.env['TEST_API_KEY'] = 'test-credential-do-not-persist'
    def prepare(spec, path, config):
        return StageResult(status='failed',env={'TEST_API_KEY':'test-credential-do-not-persist'},error_message='rejected test-credential-do-not-persist')
    profile.prepare = prepare
    config = cfg.execution_config()
    config.runtime.enable_sqlite = True
    config.runtime.sqlite_path = tmp_path/'core.sqlite'
    job = run_job(JobSpec(domain='ai_index',objective='综合总结',workflow_profile='server'),config,plugin=profile,explorer=explorer)
    assert job.status == 'failed'
    assert 'test-credential' not in job.error_message
    workspace = Path(job.workspace_path)
    events = (workspace/'logs/stage_results.jsonl').read_text()
    assert '"env"' not in events and 'test-credential' not in events
    conn=sqlite3.connect(config.runtime.sqlite_path)
    assert 'test-credential' not in '\n'.join(conn.iterdump())
    conn.close()
    assert (workspace/'artifact_manifest.json').is_file()


def test_cancellation_between_source_retries(tmp_path):
    import urllib.error
    control = RunControl()
    calls=[]
    def opener(*a,**kw):
        calls.append(1)
        raise urllib.error.URLError('temporary network error')
    client=ScopeV2AIIndexClient(base_url='https://example.com',api_key='test',opener=opener,sleeper=lambda n:control.cancel())
    with activate_run(control), pytest.raises(RunCancelled):
        client.post('/news',{})
    assert len(calls) == 1


def test_shared_pi_events_are_compacted_and_recovered_model_errors_cleared():
    line=json.dumps({'type':'message_update','message':{'huge':'x'*1000},'assistantMessageEvent':{'partial':{'text':'x'*1000},'delta':'next'}})
    compact=json.loads(_compact_pi_event_line(line))
    assert 'message' not in compact and compact['assistantMessageEvent'] == {'delta':'next'}
    events=[{'type':'message_end','message':{'stopReason':'error','errorMessage':'connection terminated'}},{'type':'auto_retry_end','success':True}]
    assert _model_event_errors('\n'.join(map(json.dumps,events))) == []


def test_process_cancellation_is_per_run(tmp_path):
    control = RunControl()
    result=[]
    unrelated = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
    def execute():
        with activate_run(control):
            try:
                _run_pi_process([sys.executable,'-c','import time; time.sleep(30)'],cwd=tmp_path,env=dict(os.environ),timeout=60,log_mode='quiet')
            except RunCancelled:
                result.append('cancelled')
    thread=Thread(target=execute)
    thread.start()
    try:
        deadline=time.monotonic()+2
        while not control._processes and time.monotonic()<deadline:
            time.sleep(.01)
        assert control._processes
        control.cancel()
        thread.join(5)
        assert not thread.is_alive() and result == ['cancelled']
        assert unrelated.poll() is None
    finally:
        terminate_process(unrelated)


def test_process_group_cleanup_includes_orphaned_descendants(tmp_path):
    pidfile=tmp_path/'child.pid'
    program='import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"]); open(sys.argv[1],"w").write(str(p.pid))'
    parent=subprocess.Popen([sys.executable,'-c',program,str(pidfile)],start_new_session=True)
    parent.wait(5)
    child=int(pidfile.read_text())
    try:
        terminate_process(parent)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            path=Path(f'/proc/{child}/stat')
            if not path.exists() or path.read_text().split()[2] == 'Z':
                break
            time.sleep(.01)
        else:
            pytest.fail('owned descendant survived cancellation')
    finally:
        try: os.kill(child,signal.SIGKILL)
        except ProcessLookupError: pass
