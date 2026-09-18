from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Event

import pytest
from fastapi.testclient import TestClient

from dataelf_server.app import create_app
from dataelf_server.jobs.manager import JobManager
from dataelf_server.jobs.store import JobStore
from dataelf_server.workflows.pipeline import PipelineError, ServerPipeline
from tests.server.helpers import settings, insight
from tests.server.test_manager import wait_terminal


class BlockingPipeline:
    def __init__(self):
        self.changed = Condition()
        self.started = []
        self.releases = [Event() for _ in range(10)]
        self.active = 0
        self.peak = 0

    def run(self, **kw):
        number = int(kw['request_payload']['query'])
        with self.changed:
            self.started.append(number)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.changed.notify_all()
        try:
            assert self.releases[number].wait(10)
            if number == 0:
                raise PipelineError('pi_error', 'controlled failure')
            kw['source_trace'](f'trace-{number}')
            return [insight(number)]
        finally:
            with self.changed:
                self.active -= 1

    def wait_started(self, count):
        with self.changed:
            assert self.changed.wait_for(lambda: len(self.started) >= count, timeout=5)

    def cancel(self):
        for release in self.releases:
            release.set()


@pytest.mark.parametrize('limit', [1, 5])
def test_slots_fifo_retry_and_result_isolation(tmp_path, limit):
    cfg = settings(project_root=tmp_path, state_dir=tmp_path/'state')
    cfg.server.max_concurrent_jobs = limit
    pipeline = BlockingPipeline()
    manager = JobManager(cfg, pipeline=pipeline)
    try:
        records = [manager.submit({'query': str(i)}) for i in range(limit + 2)]
        pipeline.wait_started(limit)
        assert pipeline.peak == limit
        for record in records[:limit]:
            assert manager.get(record.job_id).status == 'running'
            assert manager.get(record.job_id).started_at
        for record in records[limit:]:
            assert manager.get(record.job_id).status == 'queued'
            assert manager.get(record.job_id).started_at is None
        # Failure frees one slot; the retry joins behind already queued work.
        pipeline.releases[0].set()
        assert wait_terminal(manager, records[0].job_id).status == 'failed'
        pipeline.wait_started(limit + 1)
        assert pipeline.started[-1] == limit
        retried = manager.retry(records[0].job_id)
        assert retried.attempt == 2 and retried.started_at is None
        assert retried.workspace_path != records[0].workspace_path
        pipeline.releases[limit].set()
        pipeline.wait_started(limit + 2)
        assert pipeline.started[-1] == limit + 1
        assert manager.get(retried.job_id).status == 'queued'
        pipeline.releases[limit + 1].set()
        pipeline.wait_started(limit + 3)
        assert pipeline.started[-1] == 0
        pipeline.cancel()
        for i, record in enumerate(records):
            result = wait_terminal(manager, record.job_id)
            if i:
                assert result.status == 'completed'
                assert result.source_trace_id == f'trace-{i}'
                assert result.insights[0]['insight_id'] == insight(i)['insight_id']
        assert pipeline.peak == limit
    finally:
        manager.close(wait=True)


def test_http_queues_sixth_job_and_shutdown_cancels_all(tmp_path):
    cfg = settings(project_root=tmp_path, state_dir=tmp_path/'state')
    pipeline = BlockingPipeline()
    manager = JobManager(cfg, pipeline=pipeline)
    app = create_app(settings=cfg, manager_factory=lambda _: manager, validate_runtime=False)
    with TestClient(app) as client:
        ids = []
        for i in range(8):
            response = client.post('/api/v1/insight/jobs', json={'query': str(i)})
            assert response.status_code == 202
            ids.append(response.json()['data']['job_id'])
        pipeline.wait_started(5)
        for i, job_id in enumerate(ids):
            data = client.get(f'/api/v1/insight/jobs/{job_id}').json()['data']
            assert data['status'] == ('running' if i < 5 else 'queued')
            assert bool(data['started_at']) == (i < 5)
            assert client.get(f'/api/v1/insight/jobs/{job_id}/result').status_code == 409
    # Context shutdown releases all blocked runs, but their late returns cannot
    # promote shutdown failures to completed, or start queued jobs.
    assert len(pipeline.started) == 5
    assert not any(worker.is_alive() for worker in manager._workers)
    reader = JobStore(cfg.database_path)
    try:
        for job_id in ids:
            record = reader.get_job(job_id)
            assert record.status == 'failed'
            assert record.error.reason == 'service_shutdown'
    finally:
        reader.close()
    # Only the last exiting worker releases the instance lock/database.
    replacement = JobManager(cfg, pipeline=BlockingPipeline())
    replacement.close(wait=True)


def test_pipeline_cancels_every_run_even_after_another_run_finishes(tmp_path, monkeypatch):
    from dataelf_server.workflows import pipeline as module
    from dataelf.discovery.contracts import DiscoveryJob
    from dataelf.discovery.run_control import RunCancelled

    cfg = settings(project_root=tmp_path, state_dir=tmp_path/'state')
    changed = Condition()
    controls = {}
    releases = [Event() for _ in range(5)]

    def fake_run(spec, config, *, plugin, explorer, control):
        with changed:
            controls[control.job_id] = control
            changed.notify_all()
        assert releases[int(control.job_id)].wait(5)
        return DiscoveryJob(job_id=control.job_id, spec=spec,
                            workspace_path=str(control.workspace_path), status='completed')

    monkeypatch.setattr(module, 'run_job', fake_run)
    monkeypatch.setattr(module, 'workspace_insights', lambda *a, **kw: [])
    pipeline = ServerPipeline(cfg, profile_factory=lambda _: object(), explorer_factory=lambda _: object())
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(pipeline.run, job_id=str(i), request_payload={'query': str(i)},
                               workspace_path=tmp_path/str(i), progress=lambda *a: None,
                               source_trace=lambda *a: None) for i in range(5)]
        try:
            with changed:
                assert changed.wait_for(lambda: len(controls) == 5, timeout=5)
            releases[0].set()
            assert futures[0].result(timeout=5) == []
            pipeline.cancel()
            controls['0'].check()  # Finished control was removed independently.
            for i in range(1, 5):
                with pytest.raises(RunCancelled):
                    controls[str(i)].check()
        finally:
            for release in releases:
                release.set()
        for future in futures:
            future.result(timeout=5)
